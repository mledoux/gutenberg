#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

from __future__ import (unicode_literals, absolute_import,
                        division, print_function)
import os
import json
import zipfile
import tempfile
import urllib

import bs4
from bs4 import BeautifulSoup
from path import path
from jinja2 import Environment, PackageLoader

import gutenberg
from gutenberg import logger, XML_PARSER, TMP_FOLDER
from gutenberg.utils import (FORMAT_MATRIX, main_formats_for,
                             get_list_of_filtered_books, exec_cmd, cd,
                             get_langs_with_count, get_lang_groups,
                             is_bad_cover, path_for_cmd)
from gutenberg.database import Book, Format, BookFormat, Author
from gutenberg.iso639 import language_name
from gutenberg.l10n import l10n_strings

jinja_env = Environment(loader=PackageLoader('gutenberg', 'templates'))

DEBUG_COUNT = []

NB_POPULARITY_STARS = 5


def get_default_context(books):
    return {
        'l10n_strings': json.dumps(l10n_strings),
        'ui_languages': ['en', 'fr'],
        'languages': get_langs_with_count(books=books),
    }


def fa_for_format(format):
    return {
        'html': "",
        'info': 'fa-info-circle',
        'epub': 'fa-book',
        'pdf': 'fa-file-pdf-o',
    }.get(format, 'fa-file-o')


def book_name_for_fs(book):
    return book.title.strip().replace('/', '-')[:230]


def urlencode(url):
    return urllib.quote(url.encode('utf-8'))


jinja_env.filters['book_name_for_fs'] = book_name_for_fs
jinja_env.filters['language_name'] = language_name
jinja_env.filters['fa_for_format'] = fa_for_format
jinja_env.filters['urlencode'] = urlencode


def tmpl_path():
    return os.path.join(path(gutenberg.__file__).parent, 'templates')


def get_list_of_all_languages():
    return list(set(list([b.language for b in Book.select(Book.language)])))


def export_all_books(static_folder,
                     download_cache,
                     languages=[],
                     formats=[],
                     only_books=[]):

    # ensure dir exist
    path(static_folder).mkdir_p()

    books = get_list_of_filtered_books(languages=languages,
                                       formats=formats,
                                       only_books=only_books)

    sz = len(list(books))
    logger.debug("\tFiltered book collection size: {}".format(sz))

    def nb_by_fmt(fmt):
        return sum([1 for book in books
                    if BookFormat.select(BookFormat, Book, Format)
                                 .join(Book).switch(BookFormat)
                                 .join(Format)
                                 .where(Book.id == book.id)
                                 .where(Format.mime == FORMAT_MATRIX.get(fmt))
                                 .count()])

    logger.debug("\tFiltered book collection, PDF: {}"
                 .format(nb_by_fmt('pdf')))
    logger.debug("\tFiltered book collection, ePUB: {}"
                 .format(nb_by_fmt('epub')))
    logger.debug("\tFiltered book collection, HTML: {}"
                 .format(nb_by_fmt('html')))

    # export to JSON helpers
    export_to_json_helpers(books=books,
                           static_folder=static_folder,
                           languages=languages,
                           formats=formats)

    # copy CSS/JS/* to static_folder
    src_folder = tmpl_path()
    for fname in ('css', 'js', 'jquery', 'favicon.ico', 'favicon.png',
                  'jquery-ui', 'datatables', 'fonts', 'l10n'):
        src = os.path.join(src_folder, fname)
        dst = os.path.join(static_folder, fname)
        if not path(fname).ext:
            path(dst).rmtree_p()
            path(src).copytree(dst)
        else:
            path(src).copyfile(dst)

    # export homepage
    template = jinja_env.get_template('index.html')
    context = get_default_context(books=books)
    context.update({'show_books': True})
    with open(os.path.join(static_folder, 'Home.html'), 'w') as f:
        f.write(template.render(**context).encode('utf-8'))

    # Compute popularity
    popbooks = books.order_by(Book.downloads.desc())
    stars_limits = [0] * NB_POPULARITY_STARS
    stars = NB_POPULARITY_STARS
    nb_downloads = popbooks[0].downloads
    for ibook in range(0, popbooks.count(), 1):
        if ibook > float(NB_POPULARITY_STARS-stars+1)/NB_POPULARITY_STARS*popbooks.count() \
           and popbooks[ibook].downloads < nb_downloads:
            stars_limits[stars-1] = nb_downloads
            stars = stars - 1
        nb_downloads = popbooks[ibook].downloads

    # export to HTML
    cached_files = os.listdir(download_cache)
    for book in books:
        book.popularity = sum(
            [int(book.downloads >= stars_limits[i])
             for i in range(NB_POPULARITY_STARS)])
        export_book_to(book=book,
                       static_folder=static_folder,
                       download_cache=download_cache,
                       cached_files=cached_files,
                       languages=languages,
                       formats=formats,
                       books=books)


def article_name_for(book, cover=False):
    cover = "_cover" if cover else ""
    title = book_name_for_fs(book)
    return "{title}{cover}.{id}.html".format(
        title=title, cover=cover, id=book.id)


def archive_name_for(book, format):
    return "{title}.{id}.{format}".format(
        title=book_name_for_fs(book),
        id=book.id, format=format)


def fname_for(book, format):
    return "{id}.{format}".format(id=book.id, format=format)


def html_content_for(book, static_folder, download_cache):

    html_fpath = os.path.join(download_cache, fname_for(book, 'html'))

    # is HTML file present?
    if not path(html_fpath).exists():
        logger.warn("Missing HTML content for #{} at {}"
                    .format(book.id, html_fpath))
        return None

    with open(html_fpath, 'r') as f:
        return f.read()


def update_html_for_static(book, html_content, epub=False):

    soup = BeautifulSoup(html_content, XML_PARSER)

    # update all <img> links from images/xxx.xxx to {id}_xxx.xxx
    if not epub:
        for img in soup.findAll('img'):
            if 'src' in img.attrs:
                img.attrs['src'] = img.attrs['src'].replace(
                    'images/', '{id}_'.format(id=book.id))

    # update all <a> links to internal HTML pages
    # should only apply to relative URLs to HTML files.
    # examples on #16816, #22889, #30021
    def replacablement_link(book, url):
        try:
            urlp, anchor = url.rsplit('#', 1)
        except ValueError:
            urlp = url
            anchor = None
        if '/' in urlp:
            return None

        if len(urlp.strip()):
            nurl = "{id}_{url}".format(id=book.id, url=urlp)
        else:
            nurl = ""

        if anchor is not None:
            return "#".join([nurl, anchor])

        return nurl

    if not epub:
        for link in soup.findAll('a'):
            new_link = replacablement_link(
                book=book, url=link.attrs.get('href', ''))
            if new_link is not None:
                link.attrs['href'] = new_link

    # Add the title
    if not epub:
        soup.title.string = book.title

    patterns = [
        ("*** START OF THE PROJECT GUTENBERG EBOOK",
         "*** END OF THE PROJECT GUTENBERG EBOOK"),

        ("***START OF THE PROJECT GUTENBERG EBOOK",
         "***END OF THE PROJECT GUTENBERG EBOOK"),

        ("<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>",
         "<><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>"),

        # ePub only
        ("*** START OF THIS PROJECT GUTENBERG EBOOK",
         "*** START: FULL LICENSE ***"),
        ("*END THE SMALL PRINT! FOR PUBLIC DOMAIN ETEXT",
         "——————————————————————————-"),

        ("*** START OF THIS PROJECT GUTENBERG EBOOK",
         "*** END OF THIS PROJECT GUTENBERG EBOOK"),

        ("***START OF THE PROJECT GUTENBERG",
         "***END OF THE PROJECT GUTENBERG EBOOK"),

        ("COPYRIGHT PROTECTED ETEXTS*END*",
         "==========================================================="),

        ("Nous remercions la Bibliothèque Nationale de France qui a mis à",
         "The Project Gutenberg Etext of"),
        ("Nous remercions la Bibliothèque Nationale de France qui a mis à",
         "End of The Project Gutenberg EBook"),

        ("=========================================================================",
         "——————————————————————————-"),

        ("Project Gutenberg Etext", "End of Project Gutenberg Etext"),

        ("Text encoding is iso-8859-1", "Fin de Project Gutenberg Etext"),

        ("—————————————————-", "Encode an ISO 8859/1 Etext into LaTeX or HTML"),
    ]

    body = soup.find('body')
    try:
        is_encapsulated_in_div = sum(
            [1 for e in body.children
             if not isinstance(e, bs4.NavigableString)]) == 1
    except:
        is_encapsulated_in_div = False

    if is_encapsulated_in_div and not epub:
        DEBUG_COUNT.append((book.id, book.title))

    if not is_encapsulated_in_div:
        for start_of_text, end_of_text in patterns:
            if start_of_text not in body.text and end_of_text not in body.text:
                continue

            if start_of_text in body.text and end_of_text in body.text:
                remove = True
                for child in body.children:
                    if isinstance(child, bs4.NavigableString):
                        continue
                    if end_of_text in getattr(child, 'text', ''):
                        remove = True
                    if start_of_text in getattr(child, 'text', ''):
                        child.decompose()
                        remove = False
                    if remove:
                        child.decompose()
                break

            elif start_of_text in body.text:
                # logger.debug("FOUND START: {}".format(start_of_text))
                remove = True
                for child in body.children:
                    if isinstance(child, bs4.NavigableString):
                        continue
                    if start_of_text in getattr(child, 'text', ''):
                        child.decompose()
                        remove = False
                    if remove:
                        child.decompose()
                break
            elif end_of_text in body.text:
                # logger.debug("FOUND END: {}".format(end_of_text))
                remove = False
                for child in body.children:
                    if isinstance(child, bs4.NavigableString):
                        continue
                    if end_of_text in getattr(child, 'text', ''):
                        remove = True
                    if remove:
                        child.decompose()
                break

    # build infobox
    if not epub:
        infobox = jinja_env.get_template('book_infobox.html')
        infobox_html = infobox.render({'book': book})
        info_soup = BeautifulSoup(infobox_html)
        body.insert(0, info_soup.find('div'))

    # if there is no charset, set it to utf8
    if not epub and not soup.encoding:
        utf = '<meta http-equiv="Content-Type" content="text/html;' \
              ' charset=UTF-8" />'
        # title = soup.find('title')
        # title.insert_before(utf)
        utf = '<head>{}'.format(utf)

        return soup.encode().replace(str('<head>'), str(utf))

    return soup.encode()


def cover_html_content_for(book, static_folder, books):
    cover_img = "{id}_cover.jpg".format(id=book.id)
    cover_img = cover_img \
        if path(os.path.join(static_folder, cover_img)).exists() else None
    translate_author = ' data-l10n-id="author-{id}"' \
        .format(id=book.author.name().lower()) \
        if book.author.name() in ['Anonymous', 'Various'] else ''
    translate_license = ' data-l10n-id="license-{id}"' \
        .format(id=book.license.slug.lower()) \
        if book.license.slug in ['PD', 'Copyright'] else ''
    context = get_default_context(books=books)
    context.update({
        'book': book,
        'cover_img': cover_img,
        'formats': main_formats_for(book),
        'translate_author': translate_author,
        'translate_license': translate_license
    })
    template = jinja_env.get_template('cover_article.html')
    return template.render(**context)


def export_book_to(book,
                   static_folder, download_cache,
                   cached_files, languages, formats, books):
    logger.info("\tExporting Book #{id}.".format(id=book.id))

    # actual book content, as HTML
    html = html_content_for(book=book,
                            static_folder=static_folder,
                            download_cache=download_cache)
    if html:
        article_fpath = os.path.join(static_folder, article_name_for(book))
        logger.info("\t\tExporting to {}".format(article_fpath))
        try:
            new_html = update_html_for_static(book=book, html_content=html)
        except:
            new_html = html
        with open(article_fpath, 'w') as f:
            f.write(new_html)

    def symlink_from_cache(fname, dstfname=None):
        src = os.path.join(path(download_cache).abspath(), fname)
        if dstfname is None:
            dstfname = fname
        dst = os.path.join(path(static_folder).abspath(), dstfname)
        logger.info("\t\tSymlinking {}".format(dst))
        path(dst).unlink_p()
        try:
            path(src).link(dst)  # hard link
        except IOError:
            logger.error("/!\ Unable to symlink missing file {}".format(src))
            return

    def copy_from_cache(fname, dstfname=None):
        src = os.path.join(path(download_cache).abspath(), fname)
        if dstfname is None:
            dstfname = fname
        dst = os.path.join(path(static_folder).abspath(), dstfname)
        logger.info("\t\tCopying {}".format(dst))
        path(dst).unlink_p()
        try:
            path(src).copy(dst)
        except IOError:
            logger.error("/!\ Unable to copy missing file {}".format(src))
            return

    def optimize_image(fpath):
        if path(fpath).ext == '.png':
            return optimize_png(fpath)
        if path(fpath).ext in ('.jpg', '.jpeg'):
            return optimize_jpeg(fpath)
        if path(fpath).ext == '.gif':
            return optimize_gif(fpath)
        return fpath

    def optimize_gif(fpath):
        exec_cmd('gifsicle -O3 "{path}" -o "{path}"'.format(path=fpath))

    def optimize_png(fpath):
        pngquant = 'pngquant --nofs --force --ext=".png" "{path}"'
        advdef = 'advdef -z -4 -i 5 "{path}"'
        exec_cmd(pngquant.format(path=fpath))
        exec_cmd(advdef.format(path=fpath))

    def optimize_jpeg(fpath):
        exec_cmd('jpegoptim --strip-all -m50 "{path}"'.format(path=fpath))

    def optimize_epub(src, dst):
        logger.info("\t\tCreating ePUB at {}".format(dst))
        zipped_files = []
        # create temp directory to extract to
        tmpd = tempfile.mkdtemp(dir=TMP_FOLDER)
        with zipfile.ZipFile(src, 'r') as zf:
            zipped_files = zf.namelist()
            zf.extractall(tmpd)

        remove_cover = False
        for fname in zipped_files:
            fnp = os.path.join(tmpd, fname)
            if path(fname).ext in ('.png', '.jpeg', '.jpg', '.gif'):

                # special case to remove ugly cover
                if fname.endswith('cover.jpg') and is_bad_cover(fnp):
                    zipped_files.remove(fname)
                    remove_cover = True
                else:
                    optimize_image(path_for_cmd(fnp))

            if path(fname).ext in ('.htm', '.html'):
                f = open(fnp, 'r')
                html = update_html_for_static(book=book,
                                              html_content=f.read(),
                                              epub=True)
                f.close()
                with open(fnp, 'w') as f:
                    f.write(html)

            if path(fname).ext == '.ncx':
                pattern = "*** START: FULL LICENSE ***"
                f = open(fnp, 'r')
                ncx = f.read()
                f.close()
                soup = BeautifulSoup(ncx, ["lxml", "xml"])
                for tag in soup.findAll('text'):
                    if pattern in tag.text:
                        s = tag.parent.parent
                        s.decompose()
                        for s in s.next_siblings:
                            s.decompose()
                        s.next_sibling

                with open(fnp, 'w') as f:
                    f.write(soup.encode())

        # delete {id}/cover.jpg if exist and update {id}/content.opf
        if remove_cover:

            # remove cover
            path(os.path.join(tmpd, str(book.id), 'cover.jpg')).unlink_p()

            soup = None
            opff = os.path.join(tmpd, str(book.id), 'content.opf')
            if os.path.exists(opff):
                with open(opff, 'r') as fd:
                    soup = BeautifulSoup(fd.read(), ["lxml", "xml"])

                for elem in soup.findAll():
                    if getattr(elem, 'attrs', {}).get('href') == 'cover.jpg':
                        elem.decompose()

                with(open(opff, 'w')) as fd:
                    fd.write(soup.encode())

        with cd(tmpd):
            exec_cmd('zip -q0X "{dst}" mimetype'.format(dst=path_for_cmd(dst)))
            exec_cmd('zip -qXr9D "{dst}" {files}'
                     .format(dst=path_for_cmd(dst),
                             files=" ".join([f for f in zipped_files
                                             if not f == 'mimetype'])))

        path(tmpd).rmtree_p()

    def handle_companion_file(fname, dstfname=None, book=None):
        src = os.path.join(path(download_cache).abspath(), fname)
        if dstfname is None:
            dstfname = fname
        dst = os.path.join(path(static_folder).abspath(), dstfname)

        # optimization based on mime/extension
        if path(fname).ext in ('.png', '.jpg', '.jpeg', '.gif'):
            copy_from_cache(src, dst)
            optimize_image(path_for_cmd(dst))
        elif path(fname).ext == '.epub':
            tmp_epub = tempfile.NamedTemporaryFile(suffix='.epub',
                                                   dir=TMP_FOLDER)
            tmp_epub.close()
            optimize_epub(src, tmp_epub.name)
            path(tmp_epub.name).move(dst)
        else:
            # excludes files created by Windows Explorer
            if src.endswith('_Thumbs.db'):
                return
            # copy otherwise (PDF mostly)
            logger.debug("\t\tshitty ext: {}".format(dst))
            copy_from_cache(src, dst)

    # associated files (images, etc)
    for fname in [fn for fn in cached_files
                  if fn.startswith("{}_".format(book.id))]:

        if path(fname).ext in ('.html', '.htm'):
            src = os.path.join(path(download_cache).abspath(), fname)
            dst = os.path.join(path(static_folder).abspath(), fname)

            logger.info("\t\tExporting HTML file to {}".format(dst))
            html = "CAN'T READ FILE"
            with open(src, 'r') as f:
                html = f.read()
            new_html = update_html_for_static(book=book, html_content=html)
            with open(dst, 'w') as f:
                f.write(new_html)
        else:
            logger.info("\t\tCopying companion file to {}".format(fname))
            try:
                handle_companion_file(fname)
            except Exception as e:
                logger.error("\t\tException while handling companion file: {}"
                             .format(e))

    # other formats
    for format in formats:
        if format not in book.formats() or format == 'html':
            continue
        logger.info("\t\tCopying format file to {}"
                    .format(archive_name_for(book, format)))
        try:
            handle_companion_file(fname_for(book, format),
                                  archive_name_for(book, format))
        except Exception as e:
            logger.error("\t\tException while handling companion file: {}"
                         .format(e))

    # book presentation article
    cover_fpath = os.path.join(static_folder,
                               article_name_for(book=book, cover=True))
    logger.info("\t\tExporting to {}".format(cover_fpath))
    html = cover_html_content_for(book=book,
                                  static_folder=static_folder,
                                  books=books)
    with open(cover_fpath, 'w') as f:
        f.write(html.encode('utf-8'))


def authors_from_ids(idlist):
    ''' build a list of Author objects based on a list of author.gut_id

        Used to overcome large SELECT IN SQL stmts which peewee complains
        about. Slower !! '''
    authors = []
    for author in Author.select().order_by(Author.last_name.asc(),
                                           Author.first_names.asc()):
        if author.gut_id not in idlist:
            continue
        if author in authors:
            continue
        authors.append(author)
    return authors


def export_to_json_helpers(books, static_folder, languages, formats):

    def dumpjs(col, fn, var='json_data'):
        with open(os.path.join(static_folder, fn), 'w') as f:
            f.write("var {var} = ".format(var=var))
            f.write(json.dumps(col))
            f.write(";")
            # json.dump(col, f)

    # all books sorted by popularity
    logger.info("\t\tDumping full_by_popularity.js")
    dumpjs([book.to_array()
            for book in books.order_by(Book.downloads.desc())],
           'full_by_popularity.js')

    # all books sorted by title
    logger.info("\t\tDumping full_by_title.js")
    dumpjs([book.to_array()
            for book in books.order_by(Book.title.asc())],
           'full_by_title.js')

    avail_langs = get_langs_with_count(books=books)

    all_filtered_authors = []

    # language-specific collections
    for lang_name, lang, lang_count in avail_langs:
        lang_filtered_authors = list(
            set([book.author.gut_id for book in books.filter(language=lang)]))
        for aid in lang_filtered_authors:
            if aid not in all_filtered_authors:
                all_filtered_authors.append(aid)

        # by popularity
        logger.info("\t\tDumping lang_{}_by_popularity.js".format(lang))
        dumpjs(
            [book.to_array()
             for book in books.where(Book.language == lang)
                              .order_by(Book.downloads.desc())],
            'lang_{}_by_popularity.js'.format(lang))
        # by title
        logger.info("\t\tDumping lang_{}_by_title.js".format(lang))
        dumpjs(
            [book.to_array()
             for book in books.where(Book.language == lang)
                              .order_by(Book.title.asc())],
            'lang_{}_by_title.js'.format(lang))

        authors = authors_from_ids(lang_filtered_authors)
        logger.info("\t\tDumping authors_lang_{}.js".format(lang))
        dumpjs([author.to_array() for author in authors],
               'authors_lang_{}.js'.format(lang), 'authors_json_data')

    # author specific collections
    authors = authors_from_ids(all_filtered_authors)
    for author in authors:

        # all_filtered_authors.remove(author.gut_id)
        # by popularity
        logger.info(
            "\t\tDumping auth_{}_by_popularity.js".format(author.gut_id))
        dumpjs(
            [book.to_array()
             for book in books.where(Book.author == author)
                              .order_by(Book.downloads.desc())],
            'auth_{}_by_popularity.js'.format(author.gut_id))
        # by title
        logger.info("\t\tDumping auth_{}_by_title.js".format(author.gut_id))
        dumpjs(
            [book.to_array()
             for book in books.where(Book.author == author)
                              .order_by(Book.title.asc())],
            'auth_{}_by_title.js'.format(author.gut_id))

    # authors list sorted by name
    logger.info("\t\tDumping authors.js")
    dumpjs([author.to_array() for author in authors],
           'authors.js', 'authors_json_data')

    # languages list sorted by code
    logger.info("\t\tDumping languages.js")
    dumpjs(avail_langs, 'languages.js', 'languages_json_data')

    # languages by weight
    main_languages, other_languages = get_lang_groups(books)
    logger.info("\t\tDumping main_languages.js")
    dumpjs(main_languages, 'main_languages.js', 'main_languages_json_data')
    dumpjs(other_languages, 'other_languages.js', 'other_languages_json_data')
