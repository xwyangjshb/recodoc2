from __future__ import unicode_literals
import subprocess
import time
import os
import logging
from collections import defaultdict
from functools import partial
from lxml import etree
import enchant
from py4j.java_gateway import JavaGateway
from django.conf import settings
from django.db import transaction
from codeutil.parser import is_valid_match, find_parent_reference,\
        create_match
from codeutil.xml_element import XMLStrategy, XML_LANGUAGE, is_xml_snippet,\
        is_xml_lines
from codeutil.java_element import ClassMethodStrategy, MethodStrategy,\
        FieldStrategy, OtherStrategy, AnnotationStrategy, SQLFilter,\
        BuilderFilter, JAVA_LANGUAGE, is_java_snippet, is_java_lines,\
        is_exception_trace_lines, JAVA_EXCEPTION_TRACE, clean_java_name
from codeutil.other_element import FileStrategy, IgnoreStrategy,\
        IGNORE_KIND, EMAIL_PATTERN_RE, URL_PATTERN_RE, OTHER_LANGUAGE,\
        is_empty_lines, is_log_lines, LOG_LANGUAGE
from codeutil.reply_element import REPLY_LANGUAGE, is_reply_lines,\
        is_reply_header, STOP_LANGUAGE, is_rest_reply
from docutil.str_util import tokenize, find_sentence, find_paragraph, split_pos
from docutil.cache_util import get_value, get_codebase_key
from docutil.commands_util import mkdir_safe, import_clazz, download_html_tree
from docutil.progress_monitor import CLILockProgressMonitor, CLIProgressMonitor
from docutil import cache_util
from project.models import ProjectRelease, Project
from project.actions import CODEBASE_PATH
from codebase.models import CodeBase, CodeElementKind, CodeElement,\
        SingleCodeReference, CodeSnippet, CodeElementFilter, ReleaseLinkSet,\
        CodeElementFamily
from codebase.parser.java_diff import JavaDiffer
import codebase.parser.family_coverage as fcoverage


PROJECT_FILE = '.project'
CLASSPATH_FILE = '.classpath'
BIN_FOLDER = 'bin'
SRC_FOLDER = 'src'
LIB_FOLDER = 'lib'

PARSERS = dict(settings.CODE_PARSERS, **settings.CUSTOM_CODE_PARSERS)

SNIPPET_PARSERS = dict(
        settings.CODE_SNIPPET_PARSERS,
        **settings.CUSTOM_CODE_SNIPPET_PARSERS)

LINKERS = dict(settings.LINKERS, **settings.CUSTOM_LINKERS)


PREFIX_CODEBASE_CODE_WORDS = settings.CACHE_MIDDLEWARE_KEY_PREFIX +\
                                'cb_codewords'
PREFIX_PROJECT_CODE_WORDS = settings.CACHE_MIDDLEWARE_KEY_PREFIX +\
                                'project_codewords'

PREFIX_CODEBASE_FILTERS = settings.CACHE_MIDDLEWARE_KEY_PREFIX +\
                                'cb_filters'

JAVA_KINDS_HIERARCHY = {'field': 'class',
                        'method': 'class',
                        'method parameter': 'method'}

XML_KINDS_HIERARCHY = {'xml attribute': 'xml element',
                       'xml attribute value': 'xml attribute'}

ALL_KINDS_HIERARCHIES = dict(JAVA_KINDS_HIERARCHY, **XML_KINDS_HIERARCHY)

# Constants used by filter
xtext = etree.XPath("string()")

xpackage = etree.XPath("//h2")

xmember_tables = etree.XPath("//body/table")

xmembers = etree.XPath("tr/td[1]")

logger = logging.getLogger("recodoc.codebase.actions")


def start_eclipse():
    eclipse_call = settings.ECLIPSE_COMMAND
    p = subprocess.Popen([eclipse_call])
    print('Process started: {0}'.format(p.pid))
    time.sleep(7)
    check_eclipse()

    return p.pid


def stop_eclipse():
    gateway = JavaGateway()
    try:
        gateway.entry_point.closeEclipse()
        time.sleep(1)
        gateway.shutdown()
    except Exception:
        pass
    try:
        gateway.close()
    except Exception:
        pass


def check_eclipse():
    '''Check that Eclipse is started and that recodoc can communicate with
       it.'''
    gateway = JavaGateway()
    try:
        success = gateway.entry_point.getServer().getListeningPort() > 0
    except Exception:
        success = False

    if success:
        print('Connection to Eclipse: OK')
    else:
        print('Connection to Eclipse: ERROR')

    gateway.close()

    return success


def get_codebase_path(pname, bname='', release='', root=False):
    project_key = pname + bname + release
    basepath = settings.PROJECT_FS_ROOT
    if not root:
        return os.path.join(basepath, pname, CODEBASE_PATH, project_key)
    else:
        return os.path.join(basepath, pname, CODEBASE_PATH)


def create_code_db(pname, bname, release):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codeBase = CodeBase(name=bname, project_release=prelease)
    codeBase.save()

    return codeBase


def create_code_local(pname, bname, release):
    '''Create an Eclipse Java Project on the filesystem.'''
    project_key = pname + bname + release
    codebase_path = get_codebase_path(pname, bname, release)
    mkdir_safe(codebase_path)

    with open(os.path.join(codebase_path, PROJECT_FILE), 'w') as project_file:
        project_file.write("""<?xml version="1.0" encoding="UTF-8"?>
<projectDescription>
    <name>{0}</name>
    <comment></comment>
    <projects>
    </projects>
    <buildSpec>
        <buildCommand>
            <name>org.eclipse.jdt.core.javabuilder</name>
            <arguments>
            </arguments>
        </buildCommand>
    </buildSpec>
    <natures>
        <nature>org.eclipse.jdt.core.javanature</nature>
    </natures>
</projectDescription>
""".format(project_key))

    with open(os.path.join(codebase_path, CLASSPATH_FILE), 'w') as \
        classpath_file:
        classpath_file.write("""<?xml version="1.0" encoding="UTF-8"?>
<classpath>
    <classpathentry kind="src" path="src"/>
    <classpathentry kind="con" path="org.eclipse.jdt.launching.JRE_CONTAINER"/>
    <classpathentry kind="output" path="bin"/>
</classpath>
""")

    mkdir_safe(os.path.join(codebase_path, SRC_FOLDER))
    mkdir_safe(os.path.join(codebase_path, BIN_FOLDER))
    mkdir_safe(os.path.join(codebase_path, LIB_FOLDER))


def link_eclipse(pname, bname, release):
    '''Add the Java Project created with create_code_local to the Eclipse
       workspace.'''
    project_key = pname + bname + release
    codebase_path = get_codebase_path(pname, bname, release)

    gateway = JavaGateway()
    workspace = gateway.jvm.org.eclipse.core.resources.ResourcesPlugin.\
            getWorkspace()
    root = workspace.getRoot()
    path = gateway.jvm.org.eclipse.core.runtime.Path(os.path.join(
        codebase_path, PROJECT_FILE))
    project_desc = workspace.loadProjectDescription(path)
    new_project = root.getProject(project_key)
    nmonitor = gateway.jvm.org.eclipse.core.runtime.NullProgressMonitor()
#    gateway.jvm.py4j.GatewayServer.turnLoggingOn()
    # To avoid workbench problem (don't know why it needs some time).
    time.sleep(1)
    new_project.create(project_desc, nmonitor)
    new_project.open(nmonitor)
    gateway.close()


def list_code_db(pname):
    code_bases = []
    for code_base in CodeBase.objects.\
            filter(project_release__project__dir_name=pname):
        code_bases.append('{0}: {1} ({2})'.format(
            code_base.pk,
            code_base.project_release.project.dir_name,
            code_base.project_release.release))
    return code_bases


def list_code_local(pname):
    basepath = settings.PROJECT_FS_ROOT
    code_path = os.path.join(basepath, pname, CODEBASE_PATH)
    local_code_bases = []
    for member in os.listdir(code_path):
        if os.path.isdir(os.path.join(code_path, member)):
            local_code_bases.append(member)
    return local_code_bases


@transaction.commit_on_success
def create_code_element_kinds():
    kinds = []

    #NonType
    kinds.append(CodeElementKind(kind='package', is_type=False))

    # Type
    kinds.append(CodeElementKind(kind='class', is_type=True))
    kinds.append(CodeElementKind(kind='annotation', is_type=True))
    kinds.append(CodeElementKind(kind='enumeration', is_type=True))
#    kinds.append(CodeElementKind(kind='interface', is_type = True))

    # Members
    kinds.append(CodeElementKind(kind='method'))
    kinds.append(CodeElementKind(kind='method family'))
    kinds.append(CodeElementKind(kind='method parameter', is_attribute=True))
    kinds.append(CodeElementKind(kind='field'))
    kinds.append(CodeElementKind(kind='enumeration value'))
    kinds.append(CodeElementKind(kind='annotation field'))

    # XML
    kinds.append(CodeElementKind(kind='xml type', is_type=True))
    kinds.append(CodeElementKind(kind='xml element'))
    kinds.append(CodeElementKind(kind='xml attribute', is_attribute=True))
    kinds.append(CodeElementKind(kind='xml attribute value', is_value=True))
    kinds.append(CodeElementKind(kind='xml element type', is_type=True))
    kinds.append(CodeElementKind(kind='xml attribute type', is_type=True))
    kinds.append(CodeElementKind(kind='xml attribute value type',
        is_type=True))
    kinds.append(CodeElementKind(kind='property type', is_type=True))
    kinds.append(CodeElementKind(kind='property name'))
    kinds.append(CodeElementKind(kind='property value', is_value=True))

    #Files
    kinds.append(CodeElementKind(kind='xml file', is_file=True))
    kinds.append(CodeElementKind(kind='ini file', is_file=True))
    kinds.append(CodeElementKind(kind='conf file', is_file=True))
    kinds.append(CodeElementKind(kind='properties file', is_file=True))
    kinds.append(CodeElementKind(kind='log file', is_file=True))
    kinds.append(CodeElementKind(kind='jar file', is_file=True))
    kinds.append(CodeElementKind(kind='java file', is_file=True))
    kinds.append(CodeElementKind(kind='python file', is_file=True))
    kinds.append(CodeElementKind(kind='hbm file', is_file=True))

    # Other
    kinds.append(CodeElementKind(kind='unknown'))

    for kind in kinds:
        kind.save()


@transaction.autocommit
def parse_code(pname, bname, release, parser_name, opt_input=None):
    '''

    autocommit is necessary here to prevent goofs. Parsers can be
    multi-threaded and transaction management in django uses thread local...
    '''
    project_key = pname + bname + release
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]

    parser_cls_name = PARSERS[parser_name]
    parser_cls = import_clazz(parser_cls_name)
    parser = parser_cls(codebase, project_key, opt_input)
    parser.parse(CLILockProgressMonitor())

    return codebase


@transaction.autocommit
def parse_snippets(pname, source, parser_name):
    project = Project.objects.get(dir_name=pname)
    parser_cls_name = SNIPPET_PARSERS[parser_name]
    parser_cls = import_clazz(parser_cls_name)
    snippet_parser = parser_cls(project, source)
    snippet_parser.parse(CLILockProgressMonitor())


def clear_snippets(pname, language, source):
    project = Project.objects.get(dir_name=pname)
    to_delete = SingleCodeReference.objects.\
            filter(snippet__language=language).\
            filter(source=source).\
            filter(project=project)
    print('Snippets to delete: %i' % to_delete.count())
    to_delete.delete()


def clear_code_elements(pname, bname, release, parser_name='-1'):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    query = CodeElement.objects.filter(codebase=codebase)
    if parser_name != '-1':
        query = query.filter(parser=parser_name)
    query.delete()


def diff_codebases(pname, bname, release1, release2):
    prelease1 = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release1)[0]
    codebase_from = CodeBase.objects.filter(project_release=prelease1).\
            filter(name=bname)[0]
    prelease2 = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release2)[0]
    codebase_to = CodeBase.objects.filter(project_release=prelease2).\
            filter(name=bname)[0]

    # Maybe later, this will be more generic
    differ = JavaDiffer()
    return differ.diff(codebase_from, codebase_to)


def create_filter_file(file_path, url):
    new_file_path = os.path.join(settings.PROJECT_FS_ROOT, file_path)
    if os.path.exists(new_file_path):
        mode = 'a'
    else:
        mode = 'w'

    with open(new_file_path, mode) as afile:
        tree = download_html_tree(url)
        package_name = get_package_name(tree)
        tables = xmember_tables(tree)
        for table in tables[1:-1]:
            for member in xmembers(table):
                member_string = "{0}.{1}".format(package_name, xtext(member))
                afile.write(member_string + '\n')
                print(member_string)


def add_filter(pname, bname, release, filter_files):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    count = countfilter = 0
    for filterfile in filter_files.split(','):
        file_path = os.path.join(settings.PROJECT_FS_ROOT,
                filterfile.strip() + '.txt')
        with open(file_path) as afile:
            for line in afile.readlines():
                code_filter = CodeElementFilter(
                        codebase=codebase,
                        fqn=line.strip())
                code_filter.save()
                countfilter += 1
            count += 1
    print('Added {0} filter groups and {1} individual filters.'
            .format(count, countfilter))


def add_a_filter(pname, bname, release, filter_fqn, include_snippet=True,
        one_ref_only=False, include_member=False):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    code_filter = CodeElementFilter(
            codebase=codebase,
            fqn=filter_fqn,
            include_snippet=include_snippet,
            one_ref_only=one_ref_only,
            include_member=include_member)
    code_filter.save()


def link_code(pname, bname, release, linker_name, source, source_release=None,
        local_object_id=None):
    project = Project.objects.get(dir_name=pname)
    prelease = ProjectRelease.objects.filter(project=project).\
            filter(release=release)[0]
    if source_release is not None and source_release != '-1':
        srelease = ProjectRelease.objects.filter(project=project).\
            filter(release=source_release)[0]
    else:
        srelease = None
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    linker_cls_name = LINKERS[linker_name]
    linker_cls = import_clazz(linker_cls_name)
    linker = linker_cls(project, prelease, codebase, source, srelease)

    progress_monitor = CLIProgressMonitor(min_step=1.0)
    progress_monitor.info('Cache Count {0} miss of {1}'
            .format(cache_util.cache_miss, cache_util.cache_total))

    start = time.clock()

    linker.link_references(progress_monitor, local_object_id)

    stop = time.clock()
    progress_monitor.info('Cache Count {0} miss of {1}'
            .format(cache_util.cache_miss, cache_util.cache_total))
    progress_monitor.info('Time: {0}'.format(stop - start))


def clear_links(pname, release, source='-1'):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    query = ReleaseLinkSet.objects.filter(project_release=prelease)
    if source != '-1':
        query = query.filter(code_reference__source=source)
    query.delete()


def compute_families(pname, bname, release):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    code_elements = codebase.code_elements.all()

    progress_monitor = CLIProgressMonitor(min_step=1.0)

    dfamilies = fcoverage.compute_declaration_family(code_elements, True,
            progress_monitor)
    (hfamilies1, hfamiliesd) = fcoverage.\
            compute_hierarchy_family(code_elements, True, progress_monitor)

    fcoverage.compute_no_abstract_family(dfamilies, progress_monitor)
    fcoverage.compute_no_abstract_family(hfamilies1, progress_monitor)
    fcoverage.compute_no_abstract_family(hfamiliesd, progress_monitor)

    fcoverage.compute_token_family_second(dfamilies, progress_monitor)

    fcoverage.compute_token_family(code_elements, True, progress_monitor)


def clear_families(pname, bname, release):
    prelease = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release)[0]
    codebase = CodeBase.objects.filter(project_release=prelease).\
            filter(name=bname)[0]
    CodeElementFamily.objects.filter(codebase=codebase).delete()


def compare_coverage(pname, bname, release1, release2, source, resource_pk):
    prelease1 = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release1)[0]
    codebase1 = CodeBase.objects.filter(project_release=prelease1).\
            filter(name=bname)[0]
    prelease2 = ProjectRelease.objects.filter(project__dir_name=pname).\
            filter(release=release2)[0]
    codebase2 = CodeBase.objects.filter(project_release=prelease2).\
            filter(name=bname)[0]

    progress_monitor = CLIProgressMonitor(min_step=1.0)

    fcoverage.compare_coverage(codebase1, codebase2, source, resource_pk,
            progress_monitor)


### ACTIONS USED BY OTHER ACTIONS ###

def compute_filters(codebase):
    filters = CodeElementFilter.objects.filter(codebase=codebase).all()

    simple_filters = defaultdict(list)
    for cfilter in filters:
        simple_name = clean_java_name(cfilter.fqn)[0].lower()
        simple_filters[simple_name].append(cfilter)

    fqn_filters = {cfilter.fqn.lower(): cfilter for cfilter in filters}

    return (simple_filters, fqn_filters)


def get_filters(codebase):
    return get_value(PREFIX_CODEBASE_FILTERS,
        get_codebase_key(codebase),
        compute_filters,
        [codebase])


def get_package_name(tree):
    package_text = xtext(xpackage(tree)[0]).strip()
    return package_text[len('Package '):]


def compute_code_words(codebase):
    print('computing code words 2')
    d = enchant.Dict('en-US')

    elements = CodeElement.objects.\
            filter(codebase=codebase).\
            filter(kind__is_type=True).\
            iterator()

    code_words = set()
    for element in elements:
        simple_name = element.simple_name
        tokens = tokenize(simple_name)
        if len(tokens) > 1:
            code_words.add(simple_name.lower())
        else:
            simple_name = simple_name.lower()
            if not d.check(simple_name):
                code_words.add(simple_name)

    print('before returning from code words')

    logger.debug('Computed {0} code words for codebase {1}'.format(
        len(code_words), str(codebase)))

    return code_words


def compute_project_code_words(codebases):
    print('computing code words')
    code_words = set()
    for codebase in codebases:
        code_words.update(
                get_value(PREFIX_CODEBASE_CODE_WORDS,
                    get_codebase_key(codebase),
                    compute_code_words,
                    [codebase])
                )
    return code_words


def get_project_code_words(project):
    print('in project code words')
    codebases = CodeBase.objects.filter(project_release__project=project).all()
    print('checking cache?!')
    return get_value(
            PREFIX_PROJECT_CODE_WORDS,
            project.pk,
            compute_project_code_words,
            [codebases]
            )


def get_default_kind_dict():
    kinds = {}
    kinds['unknown'] = CodeElementKind.objects.get(kind='unknown')
    kinds['class'] = CodeElementKind.objects.get(kind='class')
    kinds['annotation'] = CodeElementKind.objects.get(kind='annotation')
    kinds['method'] = CodeElementKind.objects.get(kind='method')
    kinds['field'] = CodeElementKind.objects.get(kind='field')
    kinds['xml element'] = CodeElementKind.objects.get(kind='xml element')
    kinds['xml attribute'] = CodeElementKind.objects.get(kind='xml attribute')
    kinds['xml attribute value'] = \
    CodeElementKind.objects.get(kind='xml attribute value')
    kinds['xml file'] = CodeElementKind.objects.get(kind='xml file')
    kinds['hbm file'] = CodeElementKind.objects.get(kind='hbm file')
    kinds['ini file'] = CodeElementKind.objects.get(kind='ini file')
    kinds['conf file'] = CodeElementKind.objects.get(kind='conf file')
    kinds['properties file'] = \
            CodeElementKind.objects.get(kind='properties file')
    kinds['log file'] = CodeElementKind.objects.get(kind='log file')
    kinds['jar file'] = CodeElementKind.objects.get(kind='jar file')
    kinds['java file'] = CodeElementKind.objects.get(kind='java file')
    kinds['python file'] = CodeElementKind.objects.get(kind='python file')
    return kinds


def get_java_strategies():
    strategies = [
            FileStrategy(), XMLStrategy(), ClassMethodStrategy(),
            MethodStrategy(), FieldStrategy(), AnnotationStrategy(),
            OtherStrategy(), IgnoreStrategy([EMAIL_PATTERN_RE, URL_PATTERN_RE])
            ]

    method_strategies = [ClassMethodStrategy(), MethodStrategy()]

    class_strategies = [AnnotationStrategy(), OtherStrategy()]

    kind_strategies = {
                'method': method_strategies,
                'class': class_strategies,
                'unknown': strategies
                }

    return kind_strategies


def get_default_filters():
    filters = {
        JAVA_LANGUAGE: [SQLFilter(), BuilderFilter()],
        XML_LANGUAGE: [],
        OTHER_LANGUAGE: [],
        }

    return filters


def classify_code_snippet(text, filters):
    code = None
    try:
        if is_xml_snippet(text)[0]:
            language = XML_LANGUAGE
        elif is_java_snippet(text, filters[JAVA_LANGUAGE])[0]:
            language = JAVA_LANGUAGE
        else:
            language = OTHER_LANGUAGE

        code = CodeSnippet(
                language=language,
                snippet_text=text,
                )
        code.save()
    except Exception:
        logger.exception('Error while classifying snippet.')
    return code


def parse_text_code_words(text, code_words):
    # Because there is a chance that the FQN will match...
    priority = 1
    matches = []
    words = split_pos(text)
    for (word, start, end) in words:
        if word in code_words:
            # Because at this stage, we force it to choose one only...
            matches.append(create_match((start, end, 'class', priority)))
    return matches


def process_children_matches(text, matches, children, index, single_refs,
        kinds, kinds_hierarchies, save_index, find_context):

    for i, child in enumerate(children):
        content = text[child[0]:child[1]]
        parent_reference = find_parent_reference(child[2], single_refs,
                        kinds_hierarchies)
        child_reference = SingleCodeReference(
                content=content,
                kind_hint=kinds[child[2]],
                child_index=i,
                parent_reference=parent_reference)
        if save_index:
            child_reference.index = index
        if find_context:
            child_reference.sentence = find_sentence(text, child[0],
                    child[1])
            child_reference.paragraph = find_paragraph(text, child[0],
                    child[1])
        child_reference.save()
        single_refs.append(child_reference)


def process_matches(text, matches, single_refs, kinds, kinds_hierarchies,
        save_index, find_context, existing_refs):
    filtered = set()
    index = 0
    avoided = False

    for match in matches:
        if is_valid_match(match, matches, filtered):
            (parent, children) = match
            content = text[parent[0]:parent[1]]
            if parent[2] == IGNORE_KIND:
                avoided = True
                continue

            # This is a list of refs to avoid
            try:
                index = existing_refs.index(content)
                del(existing_refs[index])
                continue
            except ValueError:
                # That's ok, we can proceed!
                pass

            main_reference = SingleCodeReference(
                    content=content,
                    kind_hint=kinds[parent[2]])
            if save_index:
                main_reference.index = index
            if find_context:
                main_reference.sentence = find_sentence(text, parent[0],
                        parent[1])
                main_reference.paragraph = find_paragraph(text, parent[0],
                        parent[1])
            main_reference.save()
            single_refs.append(main_reference)

            # Process children
            process_children_matches(text, matches, children, index,
                    single_refs, kinds, kinds_hierarchies, save_index,
                    find_context)
            index += 1
        else:
            filtered.add(match)

    return avoided


def parse_single_code_references(text, kind_hint, kind_strategies, kinds,
        kinds_hierarchies=ALL_KINDS_HIERARCHIES, save_index=False,
        strict=False, find_context=False, code_words=None, existing_refs=None):
    single_refs = []
    matches = []

    kind_text = kind_hint.kind
    if kind_text not in kind_strategies:
        kind_text = 'unknown'

    if existing_refs is None:
        existing_refs = []

    for strategy in kind_strategies[kind_text]:
        matches.extend(strategy.match(text))

    if code_words is not None:
        matches.extend(parse_text_code_words(text, code_words))

    # Sort to get correct indices
    matches.sort(key=lambda match: match[0][0])

    avoided = process_matches(text, matches, single_refs, kinds,
            kinds_hierarchies, save_index, find_context, existing_refs)

    if len(single_refs) == 0 and not avoided and not strict:
        code = SingleCodeReference(content=text, kind_hint=kind_hint)
        code.save()
        single_refs.append(code)

    return single_refs


def get_default_p_classifiers():
    p_classifiers = []

    p_classifiers.append((is_empty_lines, REPLY_LANGUAGE))
    p_classifiers.append((is_reply_lines, REPLY_LANGUAGE))
    p_classifiers.append((is_reply_header, REPLY_LANGUAGE))
    p_classifiers.append((is_rest_reply, STOP_LANGUAGE))
    p_classifiers.append((
        partial(is_java_lines, filters=get_default_filters()[JAVA_LANGUAGE]),
        JAVA_LANGUAGE))
    p_classifiers.append((is_exception_trace_lines, JAVA_EXCEPTION_TRACE))
    p_classifiers.append((is_log_lines, LOG_LANGUAGE))
    p_classifiers.append((is_xml_lines, XML_LANGUAGE))

    return p_classifiers
