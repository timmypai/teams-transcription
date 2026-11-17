"""Exact Teams join paths; all URL and authorization checks remain at admission."""
import re


def teams_join_path_allowed(path):
    return isinstance(path, str) and re.fullmatch(
        r"/(?:l/meetup-join/[^/]+(?:/0|/)?|meet/[^/]+/?)", path) is not None
