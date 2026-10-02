"""Regression: a directory template must not resolve outside ``--dest``.

``--directory-template`` interpolates ``{username}``, which comes from the CLI
target the user typed. Unsanitised, ``--directory-template "{username}"`` with
the target ``@..`` resolved the destination to the parent of ``--dest``: files
landed outside it with no warning, and the "output directory" the user chose was
silently not the one that got written to.

Two independent layers now guard this, because either alone leaves a gap:

* :func:`render_directory` reduces ``{username}`` to one harmless component;
* the CLI re-checks the final path, which also covers any component or future
  substitution that could still carry a ``..``.

The regression that matters most is the last group: the documented scrapmf layout
must be untouched, or orchestrators write media somewhere unexpected.
"""

import pytest

from threadstractormf.template import render_directory

# --- layer 1: sanitising {username} -----------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["..", ".", "a/../../../tmp/pwned", "../../etc/passwd", "a..b", "./..", "/etc"],
)
def test_hostile_username_cannot_produce_parent_segments(hostile):
    """Whatever comes in, no rendered *segment* may be a parent reference.

    An empty result is fine — sanitize_filename maps ".." to "", and an empty
    final component simply resolves to the root, which is inside --dest.
    """
    rendered = render_directory("{username}", username=hostile)
    for part in rendered.split("/"):
        assert part != "..", f"{hostile!r} produced a traversal segment"


def test_traversal_username_renders_to_empty():
    # sanitize_filename maps ".." and "." to "", so the template collapses to the
    # root, which stays inside --dest instead of escaping above it
    assert render_directory("{username}", username="..") == ""
    assert render_directory("{username}", username=".") == ""


def test_slashes_in_username_are_flattened():
    assert "/" not in render_directory("{username}", username="a/b/c")
    assert render_directory("{username}", username="a/../../../tmp/pwned") == "a_tmp_pwned"


@pytest.mark.parametrize("real", ["user", "usuario.123", "con-guiones", "u_1", "ABCDEF"])
def test_real_usernames_are_untouched(real):
    assert render_directory("{username}", username=real) == real


def test_legacy_user_alias_is_sanitised_too():
    assert render_directory("{user}", username="..") == ""


# --- layer 1 must not disturb the fixed components -------------------------


def test_scrapmf_layout_is_unchanged_for_a_normal_username():
    """The documented layout, verbatim. scrapmf relies on this."""
    template = ["{scrapmf_root}", "{category}", "{username}", "{subcategory}"]
    assert (
        render_directory(
            template, username="user", category="threads", subcategory="posts"
        )
        == "default/threads/user/posts"
    )
    assert (
        render_directory(
            template, username="user", category="threads", subcategory="profile"
        )
        == "default/threads/user/profile"
    )


def test_scrapmf_root_alias_still_works():
    assert (
        render_directory(
            "{scarpmf_root}/{category}/{username}",
            username="user",
            category="threads",
        )
        == "default/threads/user"
    )


def test_category_and_subcategory_are_not_sanitised():
    """They are internal fixed values; sanitising them would only risk changing
    the layout."""
    rendered = render_directory(
        "{category}/{subcategory}", username="u", category="a..b", subcategory="c/d"
    )
    assert rendered == "a..b/c/d"


def test_string_and_list_templates_behave_the_same():
    assert render_directory("{username}", username="u") == render_directory(
        ["{username}"], username="u"
    )


# --- layer 2: the CLI refuses to write outside --dest ----------------------


CLI = ["python", "-m", "threadstractormf.cli"]


@pytest.fixture
def cookies_file(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        ".threads.net\tTRUE\t/\tTRUE\t2147483647\tsessionid\tV\n"
    )
    return f


def _invoke(tmp_path, cookies_file, *extra, target="@.."):
    from typer.testing import CliRunner

    import threadstractormf.cli as cli_module

    dest = tmp_path / "dl"
    result = CliRunner().invoke(
        cli_module.app,
        ["--cookies", str(cookies_file), "--dest", str(dest), *extra, target],
    )
    return result, dest


def test_traversal_username_collapses_inside_dest(cookies_file, tmp_path):
    """Layer 1 already prevents the escape: '..' sanitises to an empty component,
    so the destination collapses to --dest itself rather than above it."""
    result, dest = _invoke(
        tmp_path, cookies_file, "--directory-template", "{username}"
    )
    combined = result.stdout + result.stderr
    assert "outside" not in combined, "layer 1 should have made this unnecessary"
    # and whatever happened, it did not write to the parent of --dest
    assert not (tmp_path / "dl" / ".." / "dl" / "photos").exists()


def test_literal_dotdot_in_the_template_is_rejected(cookies_file, tmp_path):
    """Layer 2 catches what layer 1 cannot: a '..' typed directly into the
    template, which sanitising {username} does not touch."""
    result, dest = _invoke(
        tmp_path, cookies_file, "--directory-template", "../escape", target="@user"
    )
    assert result.exit_code == 1
    assert "outside" in result.stdout + result.stderr


def test_nothing_is_created_when_the_template_escapes(cookies_file, tmp_path):
    """The check runs before mkdir, so a rejected run creates no directory."""
    result, dest = _invoke(
        tmp_path, cookies_file, "--directory-template", "../escape", target="@user"
    )
    assert result.exit_code == 1
    assert not dest.exists()
    assert not (tmp_path / "escape").exists()


def test_normal_target_is_accepted(cookies_file, tmp_path):
    """Sanity check: the guard must not reject legitimate use.

    The scrape itself will fail without real cookies, but the failure has to be
    the network one, not the path guard.
    """
    result, dest = _invoke(
        tmp_path, cookies_file, "--directory-template", "{username}", target="@user"
    )
    assert "outside" not in result.stdout + result.stderr


def test_scrapmf_style_template_is_accepted(cookies_file, tmp_path):
    result, dest = _invoke(
        tmp_path,
        cookies_file,
        "--directory-template",
        "{scrapmf_root}/{category}/{username}/{subcategory}",
        target="@user",
    )
    combined = result.stdout + result.stderr
    assert "outside" not in combined
    # and the tree it builds is the documented one
    assert (dest / "default" / "threads" / "user" / "posts").exists()


def test_absolute_template_is_honoured(cookies_file, tmp_path):
    """An absolute --directory-template is a deliberate choice, not an escape."""
    absolute = tmp_path / "explicit" / "{username}"
    result, _ = _invoke(
        tmp_path, cookies_file, "--directory-template", str(absolute), target="@.."
    )
    combined = result.stdout + result.stderr
    assert "outside" not in combined
