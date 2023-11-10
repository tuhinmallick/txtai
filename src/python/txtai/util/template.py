"""
Template module
"""

from string import Formatter


class TemplateFormatter(Formatter):
    """
    Custom Formatter that requires each argument to be consumed.
    """

    def check_unused_args(self, used_args, args, kwargs):
        if difference := set(kwargs).difference(used_args):
            raise KeyError(difference)
