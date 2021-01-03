"""
Transform from string or AST representations of input, to intermediate_repr, a dictionary of form:
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
"""
import ast
from ast import (
    AnnAssign,
    Assign,
    ClassDef,
    Constant,
    Dict,
    FunctionDef,
    Module,
    NameConstant,
    Return,
    Tuple,
    get_docstring,
)
from collections import OrderedDict, deque
from functools import partial
from inspect import getdoc, getsource, isfunction, signature
from itertools import count, filterfalse
from operator import setitem
from types import FunctionType

from doctrans import get_logger
from doctrans.ast_utils import (
    find_ast_type,
    func_arg2param,
    get_function_type,
    get_value,
    is_argparse_add_argument,
    is_argparse_description,
)
from doctrans.docstring_parsers import _set_name_and_type, parse_docstring
from doctrans.emitter_utils import _parse_return, parse_out_param
from doctrans.parser_utils import (
    _inspect_process_ir_param,
    _interpolate_return,
    ir_merge,
)
from doctrans.pure_utils import rpartial, simple_types
from doctrans.source_transformer import to_code

logger = get_logger("doctrans.parse")


def class_(class_def, class_name=None, merge_inner_function=None, infer_type=False):
    """
    Converts an AST to our IR

    :param class_def: Class AST or Module AST with a ClassDef inside
    :type class_def: ```Union[Module, ClassDef]```

    :param class_name: Name of `class`. If None, gives first found.
    :type class_name: ```Optional[str]```

    :param merge_inner_function: Name of inner function to merge. If None, merge nothing.
    :type merge_inner_function: ```Optional[str]```

    :param infer_type: Whether to try inferring the typ (from the default)
    :type infer_type: ```bool```

    :return: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :rtype: ```dict```
    """
    assert not isinstance(class_def, FunctionDef)
    is_supported_ast_node = isinstance(class_def, (Module, ClassDef))
    if not is_supported_ast_node and isinstance(class_def, type):
        ir = _inspect(class_def, class_name)
        parsed_body = ast.parse(getsource(class_def).lstrip()).body[0]

        if merge_inner_function is not None:
            _merge_inner_function(
                parsed_body,
                infer_type=infer_type,
                intermediate_repr=ir,
                merge_inner_function=merge_inner_function,
            )
            return ir

        ir["_internal"] = {
            "body": list(
                filterfalse(
                    rpartial(isinstance, AnnAssign),
                    parsed_body.body,
                )
            ),
            "from_name": class_name,
            "from_type": "cls",
        }
        body_ir = class_(
            class_def=parsed_body,
            class_name=class_name,
            merge_inner_function=merge_inner_function,
        )
        ir_merge(ir, body_ir)

        return ir

    assert (
        is_supported_ast_node
    ), "Expected 'Union[Module, ClassDef]' got `{!r}`".format(type(class_def).__name__)
    class_def = find_ast_type(class_def, class_name)
    doc_str = get_docstring(class_def)
    intermediate_repr = (
        {
            "name": class_name,
            "type": "static",
            "doc": "",
            "params": OrderedDict(),
            "returns": None,
        }
        if doc_str is None
        else docstring(get_docstring(class_def).replace(":cvar", ":param"))
    )

    if "return_type" in intermediate_repr["params"]:
        intermediate_repr["returns"] = OrderedDict(
            (("return_type", intermediate_repr["params"].pop("return_type")),)
        )

    for e in class_def.body:
        if isinstance(e, AnnAssign):
            typ = to_code(e.annotation).rstrip("\n")
            val = (
                lambda v: {}
                if v is None
                else {
                    "default": v
                    if type(v).__name__ in simple_types
                    else (
                        lambda value: {
                            "{}": {} if isinstance(v, Dict) else set(),
                            "[]": [],
                            "()": (),
                        }.get(value, value)
                    )(to_code(v).rstrip("\n"))
                }
            )(get_value(get_value(e)))
            # if 'str' in typ and val: val["default"] = val["default"].strip("'")  # Unquote?
            typ_default = dict(typ=typ, **val)

            for key in "params", "returns":
                if e.target.id in (intermediate_repr[key] or iter(())):
                    intermediate_repr[key][e.target.id].update(typ_default)
                    typ_default = False
                    break

            # if typ_default:
            #     if e.target.id.endswith("kwargs") and typ_default["typ"] == "dict":
            #         typ_default["typ"] = "Optional[dict]"
            #     intermediate_repr["params"][e.target.id] = typ_default
        elif isinstance(e, Assign):
            val = get_value(e)
            if val is not None:
                val = get_value(val)
                deque(
                    map(
                        lambda target: setitem(
                            *(
                                (intermediate_repr["params"][target.id], "default", val)
                                if target.id in intermediate_repr["params"]
                                else (
                                    intermediate_repr["params"],
                                    target.id,
                                    {"default": val},
                                )
                            )
                        ),
                        e.targets,
                    ),
                    maxlen=0,
                )

    intermediate_repr.update(
        {
            "params": OrderedDict(
                map(
                    partial(_set_name_and_type, infer_type=infer_type),
                    intermediate_repr["params"].items(),
                )
            ),
            "_internal": {
                "body": list(
                    filterfalse(
                        rpartial(isinstance, (AnnAssign, Assign)), class_def.body
                    )
                ),
                "from_name": class_def.name,
                "from_type": "cls",
            },
        }
    )

    if merge_inner_function is not None:
        assert isinstance(class_def, ClassDef)
        _merge_inner_function(
            class_def,
            infer_type=infer_type,
            intermediate_repr=intermediate_repr,
            merge_inner_function=merge_inner_function,
        )

    # intermediate_repr['_internal']["body"]= list(filterfalse(rpartial(isinstance,(AnnAssign,Assign)),class_def.body))

    return intermediate_repr


def _merge_inner_function(
    class_def, infer_type, intermediate_repr, merge_inner_function
):
    """
    Merge the inner function if found within the class, with the class IR

    :param class_def: Class AST
    :type class_def: ```ClassDef```

    :param infer_type: Whether to try inferring the typ (from the default)
    :type infer_type: ```bool```

    :param intermediate_repr: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :type intermediate_repr: ```dict```

    :param merge_inner_function: Name of inner function to merge. If None, merge nothing.
    :type merge_inner_function: ```Optional[str]```

    :return: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :rtype: ```dict```
    """
    function_def = next(
        filter(
            lambda func: func.name == merge_inner_function,
            filter(rpartial(isinstance, FunctionDef), ast.walk(class_def)),
        ),
        None,
    )

    if function_def is not None:
        function_type = (
            "static" if not function_def.args.args else function_def.args.args[0].arg
        )
        inner_ir = function(
            function_def,
            function_name=merge_inner_function,
            function_type=function_type,
            infer_type=infer_type,
        )
        ir_merge(other=inner_ir, target=intermediate_repr)

    return intermediate_repr


def _inspect(obj, name):
    """
    Uses the `inspect` module to figure out the IR from the input

    :param obj: Something in memory, like a class, function, variable
    :type obj: ```Any```

    :param name: Name of the object being inspected
    :type name: ```str```

    :return: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :rtype: ```dict```
    """

    doc = getdoc(obj) or ""
    ir = docstring(doc) if doc else {}
    sig = signature(obj)
    is_function = isfunction(obj)
    if not is_function and "type" in ir:
        del ir["type"]

    ir.update(
        {
            "name": name or obj.__qualname__
            if hasattr(obj, "__qualname__")
            else obj.__name__,
            "params": OrderedDict(
                filter(
                    None,
                    map(
                        partial(_inspect_process_ir_param, sig=sig),
                        ir["params"].items(),
                    )
                    # if ir.get("params")
                    # else map(_inspect_process_sig, sig.parameters.items()),
                )
            ),
        }
    )

    parsed_body = ast.parse(getsource(obj).lstrip()).body[0]

    if is_function:
        ir["type"] = {"self": "self", "cls": "cls"}.get(
            next(iter(sig.parameters.values())).name, "static"
        )
        parser = function
    else:
        parser = class_

    other = parser(parsed_body)
    ir_merge(ir, other)
    if "return_type" in (ir.get("returns") or iter(())):
        ir["returns"] = OrderedDict(
            map(
                partial(_set_name_and_type, infer_type=False),
                ir["returns"].items(),
            )
        )

    # if ir.get("returns") and "returns" not in ir["returns"]:
    #     if sig.return_annotation is not _empty:
    #         ir["returns"]["return_type"]["typ"] = lstrip_typings("{!s}".format(sig.return_annotation))
    #
    #     return_q = deque(
    #         filter(
    #             rpartial(isinstance, ast.Return),
    #             ast.walk(parsed_body),
    #         ),
    #         maxlen=1,
    #     )
    #     if return_q:
    #         return_val = get_value(return_q.pop())
    #         ir["returns"]["return_type"]["default"] = get_value(return_val)
    #         if not isinstance(
    #             ir["returns"]["return_type"]["default"],
    #             (str, int, float, complex, ast.Num, ast.Str, ast.Constant),
    #         ):
    #             ir["returns"]["return_type"]["default"] = "```{}```".format(
    #                 to_code(ir["returns"]["return_type"]["default"]).rstrip("\n")
    #             )
    return ir


def function(function_def, infer_type=False, function_type=None, function_name=None):
    """
    Converts a method to our IR

    :param function_def: AST node for function definition
    :type function_def: ```Union[FunctionDef, FunctionType]```

    :param infer_type: Whether to try inferring the typ (from the default)
    :type infer_type: ```bool```

    :param function_type: Type of function, static is static or global method, others just become first arg
    :type function_type: ```Literal['self', 'cls', 'static']```

    :param function_name: name of function_def
    :type function_name: ```str```

    :return: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :rtype: ```dict```
    """
    if isinstance(function_def, FunctionType):
        # Dynamic function, i.e., this isn't source code; and is in your memory
        ir = _inspect(function_def, function_name)
        parsed_source = ast.parse(getsource(function_def).lstrip()).body[0]
        ir["_internal"] = {
            "body": list(
                filterfalse(rpartial(isinstance, AnnAssign), parsed_source.body)
            ),
            "from_name": parsed_source.name,
            "from_type": "cls",
        }
        return ir

    assert isinstance(
        function_def, FunctionDef
    ), "Expected 'FunctionDef' got `{!r}`".format(type(function_def).__name__)
    assert (
        function_name is None or function_def.name == function_name
    ), "Expected {!r} got {!r}".format(function_name, function_def.name)

    found_type = get_function_type(function_def)

    # Read docstring
    intermediate_repr_docstring = (
        get_docstring(function_def) if isinstance(function_def, FunctionDef) else None
    )
    if intermediate_repr_docstring is None:
        intermediate_repr = {
            "name": function_name or function_def.name,
            "params": OrderedDict(
                map(
                    func_arg2param,
                    function_def.args.args
                    if found_type == "static"
                    else function_def.args.args[1:],
                )
            ),
            "returns": None,
        }
    else:
        intermediate_repr = docstring(
            intermediate_repr_docstring.replace(":cvar", ":param"),
            infer_type=infer_type,
        )

    intermediate_repr.update(
        {
            "name": function_name or function_def.name,
            "type": function_type or found_type,
        }
    )

    if function_def.body:
        intermediate_repr["_internal"] = {
            "body": function_def.body
            if intermediate_repr_docstring is None
            else function_def.body[1:],
            "from_name": function_def.name,
            "from_type": found_type,
        }

    params_to_append = OrderedDict()
    if (
        hasattr(function_def.args, "kwarg")
        and function_def.args.kwarg
        and function_def.args.kwarg.arg in intermediate_repr["params"]
    ):
        _param = intermediate_repr["params"].pop(function_def.args.kwarg.arg)
        assert "typ" in _param
        # if "typ" not in _param:
        #     _param["typ"] = (
        #         "Optional[dict]"
        #         if function_def.args.kwarg.annotation is None
        #         else to_code(function_def.args.kwarg.annotation).rstrip("\n")
        #     )
        params_to_append[function_def.args.kwarg.arg] = _param
        del _param

    idx = count()

    # Set defaults
    if intermediate_repr["params"]:
        deque(
            map(
                lambda args_defaults: deque(
                    map(
                        lambda idxparam_idx_arg: intermediate_repr["params"][
                            idxparam_idx_arg[2].arg
                        ].update(
                            dict(
                                (
                                    {}
                                    if getattr(idxparam_idx_arg[2], "annotation", None)
                                    is None
                                    else dict(
                                        typ=to_code(
                                            idxparam_idx_arg[2].annotation
                                        ).rstrip("\n")
                                    )
                                ),
                                **(
                                    lambda _defaults: dict(
                                        default=(
                                            lambda v: (
                                                _defaults[idxparam_idx_arg[1]]
                                                if isinstance(
                                                    _defaults[idxparam_idx_arg[1]],
                                                    (NameConstant, Constant),
                                                )
                                                else v
                                            )
                                            if v is None
                                            else v
                                        )(get_value(_defaults[idxparam_idx_arg[1]]))
                                    )
                                    if idxparam_idx_arg[1] < len(_defaults)
                                    and _defaults[idxparam_idx_arg[1]] is not None
                                    else {}
                                )(getattr(function_def.args, args_defaults[1])),
                            )
                        ),
                        (
                            lambda _args: map(
                                lambda idx_arg: (next(idx), idx_arg[0], idx_arg[1]),
                                enumerate(
                                    filterfalse(
                                        lambda _arg: _arg.arg[0] == "*",
                                        (
                                            _args
                                            if found_type == "static"
                                            or args_defaults[0] == "kwonlyargs"
                                            else _args[1:]
                                        ),
                                    )
                                ),
                            )
                        )(getattr(function_def.args, args_defaults[0])),
                    ),
                    maxlen=0,
                ),
                (("args", "defaults"), ("kwonlyargs", "kw_defaults")),
            ),
            maxlen=0,
        )

    intermediate_repr["params"].update(params_to_append)
    intermediate_repr["params"] = OrderedDict(
        map(
            partial(_set_name_and_type, infer_type=infer_type),
            intermediate_repr["params"].items(),
        )
    )

    # Convention - the final top-level `return` is the default
    intermediate_repr = _interpolate_return(function_def, intermediate_repr)
    if "return_type" in (intermediate_repr.get("returns") or iter(())):
        intermediate_repr["returns"] = OrderedDict(
            map(
                partial(_set_name_and_type, infer_type=infer_type),
                intermediate_repr["returns"].items(),
            )
        )
    return intermediate_repr


def argparse_ast(function_def, function_type=None, function_name=None):
    """
    Converts an argparse AST to our IR

    :param function_def: AST of argparse function_def
    :type function_def: ```FunctionDef```

    :param function_type: Type of function, static is static or global method, others just become first arg
    :type function_type: ```Literal['self', 'cls', 'static']```

    :param function_name: name of function_def
    :type function_name: ```str```

    :return: a dictionary of form
        {  "name": Optional[str],
           "type": Optional[str],
           "doc": Optional[str],
           "params": OrderedDict[str, {'typ': str, 'doc': Optional[str], 'default': Any}]
           "returns": Optional[OrderedDict[Literal['return_type'],
                                           {'typ': str, 'doc': Optional[str], 'default': Any}),)]] }
    :rtype: ```dict```
    """
    assert isinstance(
        function_def, FunctionDef
    ), "Expected 'FunctionDef' got `{!r}`".format(type(function_def).__name__)

    doc_string = get_docstring(function_def)
    intermediate_repr = {
        "name": function_name,
        "type": function_type or get_function_type(function_def),
        "doc": "",
        "params": OrderedDict(),
    }
    ir = parse_docstring(doc_string, emit_default_doc=True)
    for node in function_def.body[1:]:
        if is_argparse_add_argument(node):
            name, _param = parse_out_param(node, emit_default_doc=False)
            (
                intermediate_repr["params"][name].update
                if name in intermediate_repr["params"]
                else partial(setitem, intermediate_repr["params"], name)
            )(_param)
        elif isinstance(node, Assign) and is_argparse_description(node):
            intermediate_repr["doc"] = get_value(node.value)
        elif isinstance(node, Return) and isinstance(node.value, Tuple):
            intermediate_repr["returns"] = OrderedDict(
                (
                    _parse_return(
                        node,
                        intermediate_repr=ir,
                        function_def=function_def,
                        emit_default_doc=False,
                    ),
                )
            )
    if len(function_def.body) > len(intermediate_repr["params"]) + 3:
        intermediate_repr["_internal"] = {
            "body": list(
                filterfalse(
                    is_argparse_description,
                    filterfalse(is_argparse_add_argument, function_def.body),
                )
            ),
            "from_name": function_def.name,
            "from_type": "static",
        }

    # if "return_type" in intermediate_repr.get("returns", {}):
    #     pp({'intermediate_repr["returns"]["return_type"]': intermediate_repr["returns"]["return_type"]})
    #     intermediate_repr["returns"]["return_type"].update = dict(
    #         interpolate_defaults(intermediate_repr["returns"]["return_type"])
    #     )

    return intermediate_repr


def docstring(doc_string, infer_type=False, return_tuple=False):
    """
    Converts a docstring to an AST

    :param doc_string: docstring portion
    :type doc_string: ```Union[str, Dict]```

    :param infer_type: Whether to try inferring the typ (from the default)
    :type infer_type: ```bool```

    :param return_tuple: Whether to return a tuple, or just the intermediate_repr
    :type return_tuple: ```bool```

    :return: intermediate_repr, whether it returns or not
    :rtype: ```Optional[Union[dict, Tuple[dict, bool]]]```
    """
    assert isinstance(doc_string, str), "Expected 'str' got {!r}".format(
        type(doc_string).__name__
    )
    parsed = (
        doc_string
        if isinstance(doc_string, dict)
        else parse_docstring(doc_string, infer_type=infer_type)
    )

    if return_tuple:
        return parsed, (
            "returns" in parsed
            and parsed["returns"] is not None
            and "return_type" in (parsed.get("returns") or iter(()))
        )

    return parsed


__all__ = [
    "argparse_ast",
    "class_",
    "docstring",
    "function",
]
