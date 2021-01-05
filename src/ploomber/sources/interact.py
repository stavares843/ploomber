"""
One of Ploomber's main goals is to allow writing robust/reliable code in an
interactive way. Interactive workflows make people more productive but they
might come in detriment of writing high quality code (e.g. developing a
pipeline in a single ipynb file). The basic idea for this module is to provide
a way to transparently go back and forth between a Task in a DAG and a
temporary Jupyter notebook. Currently, we only provide this for PythonCallable
and NotebookRunner but the idea is to expand to other tasks, so we have to
decide on a common behavior for this, here are a few rules:

1) Temporary jupyter notebook are usually destroyed when the user closes the
jupyter applciation. But there are extraordinary cases where we don't want to
remove it, as it might cause code loss. e.g. if the user calls
PythonCallable.develop() and while it is editing the notebook the module where
the source function is defined, we risk corrupting the module file, so we abort
overriding changes but still keep the temporary notebook. For this reason,
we save temporary notebooks in the same location of the source being edited,
to make it easier to recognize which file is related to.

2) The current working directory (cwd) in the session where Task.develop() is
called can be different from the cwd in the Jupyter application. This happens
because Jupyter sets the cwd to the current parent folder, this means that
any relative path defined in the DAG, will break if the cwd in the Jupyter app
is not the same as in the DAg declaration. To fix this, we always add a top
cell in temporary notebooks to make the cwd the same folder where
Task.develop() was called.

3) [TODO] all temporary cells must have a tmp- preffx


TODO: move the logic that implements NotebookRunner.{develop, debug} to this
module
"""
import importlib
from itertools import chain
from pathlib import Path
import inspect

import jupyter_client
from papermill.translators import PythonTranslator
import parso
import nbformat

from ploomber.util import chdir_code

# TODO: if imports are added and the file is saved multiple times, imports
# are duplicated
# TODO: if original file is modified, add a new function is replaced in the
# same position, when the notebook is reloaded, it loads the source code
# from that function


class CallableInteractiveDeveloper:
    """Convert callables to notebooks, edit and save back

    Parameters
    ----------
    fn : callable
        Function to edit
    params : dict
        Parameters to call the function

    Examples
    --------
    >>> wih CallableInteractiveDeveloper(fn, {'param': 1}) as path_to_nb:
    ...     # do stuff with the notebook file
    ...     pass
    """
    def __init__(self, fn, params):
        self.fn = fn
        self.path_to_source = Path(inspect.getsourcefile(fn))
        self.params = params
        self.tmp_path = self.path_to_source.with_name(
            self.path_to_source.with_suffix('').name + '-tmp.ipynb')
        self._source_code = None

    def to_nb(self, path=None):
        """
        Converts the function to is notebook representation, Returns a
        notebook object, if path is passed, it saves the notebook as well
        Returns the function's body in a notebook (tmp location), inserts
        params as variables at the top
        """
        body_elements, _ = parse_function(self.fn)
        imports_cell = extract_imports(self.fn)
        return function_to_nb(body_elements, imports_cell, self.params,
                              self.fn, path)

    def overwrite(self, obj):
        """
        Overwrite the function's body with the notebook contents, excluding
        injected parameters and cells whose first line is "#". obj can be
        either a notebook object or a path
        """
        # force to reload module to get the right information in case the
        # original source code was modified and the function is no longer in
        # the same position. NOTE: are there any  problems with this approach?
        # we could also read the dile directly and use ast/parso to get the
        # function's information we need
        mod = importlib.reload(inspect.getmodule(self.fn))
        self.fn = getattr(mod, self.fn.__name__)

        if isinstance(obj, (str, Path)):
            nb = nbformat.read(obj, as_version=nbformat.NO_CONVERT)
        else:
            nb = obj

        nb.cells = nb.cells[:last_non_empty_cell(nb.cells)]

        # remove cells that are only needed for the nb but not for the function
        code_cells = [c['source'] for c in nb.cells if keep_cell(c)]

        # add 4 spaces to each code cell, exclude white space lines
        code_cells = [indent_cell(code) for code in code_cells]

        # get the original file where the function is defined
        content = self.path_to_source.read_text()
        content_lines = content.splitlines()
        trailing_newline = content[-1] == '\n'

        fn_starts, fn_ends = function_lines(self.fn)

        # keep the file the same until you reach the function definition plus
        # an offset to account for the signature (which might span >1 line)
        _, body_start = parse_function(self.fn)
        keep_until = fn_starts + body_start
        header = content_lines[:keep_until]

        # the footer is everything below the end of the original definition
        footer = content_lines[fn_ends:]

        # if there is anything at the end, we have to add an empty line to
        # properly end the function definition, if this is the last definition
        # in the file, we don't have to add this
        if footer:
            footer = [''] + footer

        new_content = '\n'.join(header + code_cells + footer)

        # if the original file had a trailing newline, keep it
        if trailing_newline:
            new_content += '\n'

        # finally add new imports, if any
        imports_new = get_imports_new_source(nb)

        # if the cell for new imports has any content, add it at the top
        if imports_new:
            new_content = imports_new + new_content

        self.path_to_source.write_text(new_content)

    def __enter__(self):
        self._source_code = self.path_to_source.read_text()
        self.to_nb(path=self.tmp_path)
        return str(self.tmp_path)

    def __exit__(self, exc_type, exc_val, exc_tb):
        current_source_code = self.path_to_source.read_text()

        if self._source_code != current_source_code:
            raise ValueError(f'File "{self.path_to_source}" (where '
                             f'callable "{self.fn.__name__}" is defined) '
                             'changed while editing the function in the '
                             'notebook app. This might lead to corrupted '
                             'source files. Changes from the notebook were '
                             'not saved back to the module. Notebook '
                             f'available at "{self.tmp_path}')

        self.overwrite(self.tmp_path)
        Path(self.tmp_path).unlink()

    def __del__(self):
        tmp = Path(self.tmp_path)
        if tmp.exists():
            tmp.unlink()


def last_non_empty_cell(cells):
    """Returns the index + 1 for the last non-empty cell
    """
    idx = len(cells)

    for cell in cells[::-1]:
        if cell.source:
            return idx

        idx -= 1

    return idx


def keep_cell(cell):
    """
    Rule to decide whether to keep a cell or not. This is executed before
    converting the notebook back to a function
    """
    tags = set(cell['metadata'].get('tags', {}))
    tmp_tags = {
        'injected-parameters', 'imports', 'imports-new', 'debugging-settings'
    }
    has_tmp_tags = len(tags & tmp_tags)

    return (cell['cell_type'] == 'code' and not has_tmp_tags
            and cell['source'][:2] != '#\n')


def indent_line(lline):
    return '    ' + lline if lline else ''


def indent_cell(code):
    return '\n'.join([indent_line(line) for line in code.splitlines()])


def body_elements_from_source(source):
    # getsource adds a new line at the end of the the function, we don't need
    # this

    body = parso.parse(source).children[0].children[-1]

    # parso is adding a new line as first element, not sure if this
    # happens always though
    if isinstance(body.children[0], parso.python.tree.Newline):
        body_elements = body.children[1:]
    else:
        body_elements = body.children

    return body_elements, body.start_pos[0] - 1


def parse_function(fn):
    """
    Extract function's source code, parse it and return function body
    elements along with the # of the last line for the signature (which
    marks the beginning of the function's body) and all the imports
    """
    # TODO: exclude return at the end, what if we find more than one?
    # maybe do not support functions with return statements for now
    source = inspect.getsource(fn).rstrip()
    body_elements, start_pos = body_elements_from_source(source)
    return body_elements, start_pos


def extract_imports(fn):
    # get imports in the corresponding module
    module = parso.parse(Path(inspect.getfile(fn)).read_text())
    imports_statements = '\n'.join(
        [imp.get_code() for imp in module.iter_imports()])

    imports_cell = imports_statements

    # add local definitions, if any
    imports_local = make_import_from_definitions(module, fn)

    if imports_local:
        imports_cell = imports_cell + '\n' + imports_local

    return imports_cell


def function_lines(fn):
    lines, start = inspect.getsourcelines(fn)
    end = start + len(lines)
    return start, end


def get_func_and_class_names(module):
    return [
        defs.name.get_code().strip()
        for defs in chain(module.iter_funcdefs(), module.iter_classdefs())
    ]


def get_imports_new_source(nb):
    """
    Returns the source code of the first cell tagged 'imports-new', strips
    out comments
    """
    source = None

    for cell in nb.cells:
        if 'imports-new' in cell['metadata'].get('tags', {}):
            source = cell.source
            break

    if source:
        lines = [
            line for line in source.splitlines() if not line.startswith('#')
        ]

        if lines:
            return '\n'.join(lines) + '\n'


def make_import_from_definitions(module, fn):
    module_name = inspect.getmodule(fn).__name__
    names = [
        name for name in get_func_and_class_names(module)
        if name != fn.__name__
    ]

    if names:
        names_all = ', '.join(names)
        return f'from {module_name} import {names_all}'


def function_to_nb(body_elements, imports_cell, params, fn, path):
    """
    Save function body elements to a notebook
    """
    # TODO: Params should implement an option to call to_json_serializable
    # on product to avoid repetition I'm using this same code in notebook
    # runner. Also raise error if any of the params is not
    # json serializable
    try:
        params = params.to_json_serializable()
        params['product'] = params['product'].to_json_serializable()
    except AttributeError:
        pass

    nb_format = nbformat.versions[nbformat.current_nbformat]
    nb = nb_format.new_notebook()

    # get the module where the function is declared
    tokens = inspect.getmodule(fn).__name__.split('.')
    module_name = '.'.join(tokens[:-1])

    # add cell that chdirs for the current working directory
    # add __package__, we need this for relative imports to work
    # see: https://www.python.org/dev/peps/pep-0366/ for details
    source = """
# Debugging settings (this cell will be removed before saving)
# change the current working directory to the one when .debug() happen
# to make relative paths work
import os
{}
__package__ = "{}"
""".format(chdir_code(Path('.').resolve()), module_name)
    cell = nb_format.new_code_cell(source,
                                   metadata={'tags': ['debugging-settings']})
    nb.cells.append(cell)

    # then add params passed to the function
    cell = nb_format.new_code_cell(PythonTranslator.codify(params),
                                   metadata={'tags': ['injected-parameters']})
    nb.cells.append(cell)

    # first cell: add imports cell
    nb.cells.append(
        nb_format.new_code_cell(source=imports_cell,
                                metadata=dict(tags=['imports'])))

    # second cell: added imports, in case the user wants to add any imports
    # back to the original module
    imports_new_comment = (
        '# Use this cell to include any imports that you '
        'want to save back\n# to the top of the module, comments will be '
        'ignored')
    nb.cells.append(
        nb_format.new_code_cell(source=imports_new_comment,
                                metadata=dict(tags=['imports-new'])))

    for statement in body_elements:
        lines, newlines = split_statement(statement)

        # find indentation # of characters using the first line
        idx = indentation_idx(lines[0])

        # remove indentation from all function body lines
        lines = [line[idx:] for line in lines]

        # add one empty cell per leading new line
        nb.cells.extend(
            [nb_format.new_code_cell(source='') for _ in range(newlines)])

        # add actual code as a single string
        cell = nb_format.new_code_cell(source='\n'.join(lines))
        nb.cells.append(cell)

    k = jupyter_client.kernelspec.get_kernel_spec('python3')

    nb.metadata.kernelspec = {
        "display_name": k.display_name,
        "language": k.language,
        "name": 'python3'
    }

    if path:
        nbformat.write(nb, path)

    return nb


def split_statement(statement):
    code = statement.get_code()

    newlines = 0

    for char in code:
        if char != '\n':
            break

        newlines += 1

    lines = code.strip('\n').split('\n')
    return lines, newlines


def indentation_idx(line):
    idx = len(line) - len(line.lstrip())
    return idx
