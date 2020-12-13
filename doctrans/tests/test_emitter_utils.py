""" Tests for emitter_utils """
from ast import Attribute, Call, Expr, Load, Name, Subscript, keyword
from copy import deepcopy
from unittest import TestCase

from doctrans.ast_utils import set_value
from doctrans.emitter_utils import interpolate_defaults, parse_out_param
from doctrans.pure_utils import rpartial
from doctrans.tests.mocks.argparse import argparse_add_argument_ast, argparse_func_ast
from doctrans.tests.mocks.docstrings import intermediate_repr
from doctrans.tests.utils_for_tests import unittest_main


class TestEmitterUtils(TestCase):
    """ Test class for emitter_utils """

    def test_parse_out_param(self) -> None:
        """ Test that parse_out_param parses out the right dict """
        self.assertDictEqual(
            parse_out_param(
                next(filter(rpartial(isinstance, Expr), argparse_func_ast.body[::-1]))
            ),
            intermediate_repr["params"][-1],
        )

    def test_parse_out_param_default(self) -> None:
        """ Test that parse_out_param sets default when required and unset """

        self.assertDictEqual(
            parse_out_param(argparse_add_argument_ast),
            {"default": 0, "doc": None, "name": "num", "typ": "int"},
        )

    def test_parse_out_param_fails(self) -> None:
        """ Test that parse_out_param throws NotImplementedError when unsupported type given """
        self.assertRaises(
            NotImplementedError,
            lambda: parse_out_param(
                Expr(
                    Call(
                        args=[set_value("--num")],
                        func=Attribute(
                            Name("argument_parser", Load()),
                            "add_argument",
                            Load(),
                        ),
                        keywords=[
                            keyword(
                                arg="type",
                                value=Subscript(
                                    expr_context_ctx=None,
                                    expr_slice=None,
                                    expr_value=None,
                                ),
                                identifier=None,
                            ),
                            keyword(
                                arg="required",
                                value=set_value(True),
                                identifier=None,
                            ),
                        ],
                        expr=None,
                        expr_func=None,
                    )
                )
            ),
        )

    def test_interpolate_defaults(self) -> None:
        """ Test that interpolate_defaults corrects sets the default property """
        param = deepcopy(intermediate_repr["params"][2])
        param_with_correct_default = deepcopy(param)
        del param["default"]
        self.assertDictEqual(interpolate_defaults(param), param_with_correct_default)


unittest_main()
