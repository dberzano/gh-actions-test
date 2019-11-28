"""Utility functions for CI.

This is compatible with the GitHub API and can be called from GitHub Actions.
"""

import os
import os.path
import sys
import re
import json
import subprocess
import requests
from os import makedirs, rename
from shutil import rmtree
from tempfile import mkdtemp
from glob import iglob
from itertools import chain
from github import Github


class CachedGitHubActionEvent:
    """Interface to the cached GitHub Action event dictionary as found on the worker."""

    def __init__(self):
        self._event = None

    def __call__(self):
        """Read the content lazily and return it."""
        if self._event is None:
            with open(os.environ["GITHUB_EVENT_PATH"]) as gh_evt:
                self._event = json.loads(gh_evt.read())
        return self._event


class GhRequests:
    """Interface to use requests with the GitHub API."""

    def __init__(self, token):
        self._token = token
        self._headers = {"Accept": "application/vnd.github.antiope-preview+json"}

    def get(self, url, data=None):
        """HTTP GET to the given URL with the given data."""
        return requests.get(url, json=data, headers=self._headers, auth=("", self._token))

    def post(self, url, data=None):
        """HTTP POST to the given URL with the given data."""
        return requests.post(url, json=data, headers=self._headers, auth=("", self._token))


GH_EVENT = CachedGitHubActionEvent()
GH = Github(os.environ["GITHUB_TOKEN"])
GH_REQ = GhRequests(os.environ["GITHUB_TOKEN"])
JIRA_URL = "https://yabba.atlassian.net/browse"
WIP_CONTEXT = "Draft"
WIP_LABEL = "draft"
LINTERS = {"flake8-plain": {"py": ["flake8", "--statistics"],
                            "ipynb": ["flake8", "--append-config", ".flake8_append_notebooks",
                                      "--statistics"]},
           "flake8-json": {"py": ["flake8", "--format=json"],
                           "ipynb": ["flake8", "--append-config", ".flake8_append_notebooks",
                                     "--format=json"]}}


def get_pr_title():
    """Return current PR title. Do not rely on cached info, query the API."""
    repo = GH_EVENT()["pull_request"]["base"]["repo"]["full_name"]
    prnum = GH_EVENT()["pull_request"]["number"]
    pr = GH.get_repo(repo).get_pull(prnum)
    print(f"Using title from API: {pr.title}")
    cached_title = GH_EVENT()["pull_request"]["title"]
    if pr.title != cached_title:
        print(f"Cached title (debug): {cached_title}")
    return pr.title


def get_pr_labels():
    """Return a list of text labels for this PR. Use cached info."""
    return (x["name"] for x in GH_EVENT()["pull_request"]["labels"])


def get_status(context):
    """Return the most recent status for a given context of a Pull Request.

    The most recent status is the one displayed on the GitHub Pull Request interface. `None` is
    returned if no status is found.

    The returned status is a PyGithub object.
    """
    sha = GH_EVENT()["pull_request"]["head"]["sha"]
    repo = GH_EVENT()["pull_request"]["head"]["repo"]["full_name"]
    commit = GH.get_repo(repo).get_commit(sha)
    for st in commit.get_statuses():
        if st.context == context:
            return st
    return None


def post_tagged_comment(tag, body):
    """Post a new tagged comment or modify an existing one."""
    tag = re.sub('[^a-zA-Z0-9_-]', '_', tag)  # normalize
    html_tag = f"<!-- CWTag: {tag} -->"
    repo = GH_EVENT()["pull_request"]["base"]["repo"]["full_name"]
    prnum = GH_EVENT()["pull_request"]["number"]
    pr = GH.get_repo(repo).get_pull(prnum)
    found = False
    full_body = f"{html_tag}\n{body}"
    for comment in pr.get_issue_comments():
        if comment.body.startswith(html_tag):
            found = True
            if comment.body == full_body:
                print("Not updating comment: already up-to-date")
            else:
                comment.edit(full_body)
            break
    if not found:
        pr.create_issue_comment(full_body)


def check_pr_title(title=None, print_func=print):
    """Check pull request title."""
    if title is None:
        title = get_pr_title()
    m = re.search("^(([A-Z]{2,}-[1-9][0-9]*, )*([A-Z]{2,}-[1-9][0-9]*)): ", title)
    if not m:
        print_func("Pull request title must begin with the associated Jira issue, e.g.:\n\n"
                   "    JIRAKEY-42: Improve core algorithm\n\n"
                   "Rename the pull request accordingly and create a Jira if it does not exist!")
        return 1

    # Find all the Jira keys and post comment with backlinks
    jira_keys = re.findall("[A-Z]{2,}-[1-9][0-9]*", m.group(1))
    print_func(f"Pull request title is good. Jira keys found: {', '.join(jira_keys)}")
    msg = "Connected Jira: " + ", ".join(f"[{jk}]({JIRA_URL}/{jk})" for jk in jira_keys)
    post_tagged_comment("jira", msg)

    return 0


def check_wip():
    """Check if it's a WIP by checking the labels. Add custom status."""
    sha = GH_EVENT()["pull_request"]["head"]["sha"]
    repo = GH_EVENT()["pull_request"]["head"]["repo"]["full_name"]
    commit = GH.get_repo(repo).get_commit(sha)

    # Get current status
    was_wip = get_status(WIP_CONTEXT)
    if was_wip is not None:
        was_wip = was_wip.state != "success"
    else:
        was_wip = None

    is_wip = WIP_LABEL in get_pr_labels()
    print(f"Work in progress: {'yes' if is_wip else 'no'}")

    if is_wip != was_wip:
        print("Updating WIP status")
        if is_wip:
            commit.create_status("failure", description=f"Has the \"{WIP_LABEL}\" label",
                                 context=WIP_CONTEXT)
        else:
            commit.create_status("success", description=f"Does not have the \"{WIP_LABEL}\" label",
                                 context=WIP_CONTEXT)
    else:
        print("Not updating WIP status: did not change")
    return 0


def test_pr_title():
    """Test pull request title checker."""
    pr_titles = ["JiRA-123: This is a title",
                 "J-12: A title",
                 "JIRA-01: This is a title",
                 " JIRA-42: This is a title",
                 "JIRA-42:This is a title",
                 "JIRA-43: this is a title",
                 "JIRA-1, JA-2: Multiple issues",
                 "JIRA-1, JIRA-2, JIRA-3: Multiple issues"]
    for title in pr_titles:
        ret = check_pr_title(title, lambda x: None)
        print(f"[{' OK ' if ret == 0 else 'FAIL'}] {title}")


def run_silent(cmd):
    """Silently run a command. Return a tuple with the exit code and aggregated output."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = p.communicate()[0].decode("utf-8")
    return (p.returncode, output)


def lint_py():
    """Lint all `.py` files."""
    return lint(["*.py"])


def lint_notebooks():
    """Lint all notebooks."""
    return lint(["*.ipynb"])


def lint_all():
    """Lint everything (Python scripts and notebooks)."""
    return lint(["*.py", "*.ipynb"])


def lint(ext, checks_api=True):
    """Use linters to lint all the specified extensions."""
    if checks_api:
        use_linters = ["flake8-json"]
        annotations = []
    else:
        use_linters = ["flake8-plain"]

    # Create a temporary directory for converted Python notebooks
    tempdir = mkdtemp()

    bad = {}
    for fn in chain(*(iglob(f"**/{pattern}", recursive=True) for pattern in ext)):
        root_dir = fn.split("/", 1)[0]
        if root_dir in ["dist", "build", ".ipynb_checkpoints"] or root_dir.startswith("venv"):
            continue

        for linter_name, linter_opts in LINTERS.items():
            if linter_name not in use_linters:
                continue
            # If this is a Python notebook, convert it first
            if fn.endswith(".ipynb"):
                # Notebook: convert first. Check if converted exists already
                ipynb_dn = os.path.join(tempdir, os.path.dirname(fn))
                try:
                    makedirs(ipynb_dn)
                except FileExistsError:
                    pass
                real_fn = os.path.join(tempdir, fn)[:-6] + ".py"
                rc, out = run_silent(["jupyter", "nbconvert", fn, "--to", "script"])
                if rc != 0 or not os.path.isfile(fn[:-6] + ".py"):
                    print("ERROR")
                    print("\n" + out.strip("\n") + "\n")
                    raise RuntimeError("Notebook conversion failed")
                rename(fn[:-6] + ".py", real_fn)
                linter_cmd = linter_opts.get("ipynb", [linter_name])

            else:
                # Not a notebook
                real_fn = os.path.realpath(fn)
                linter_cmd = linter_opts.get("py", [linter_name])

            print(f"{fn}: {linter_cmd[0]}: ", end="")

            rc, out = run_silent(linter_cmd + [real_fn])
            if rc == 0:
                print("OK")
            else:
                print("ERROR")
                if checks_api:
                    # Assume JSON output
                    linter_out = json.loads(out)
                    linter_out = linter_out[list(linter_out.keys())[0]]
                    for li in linter_out:
                        # https://developer.github.com/v3/checks/runs/#annotations-object
                        annotations.append({
                            "path": fn,
                            "start_line": li["line_number"],
                            "end_line": li["line_number"],
                            "start_column": li["column_number"],
                            "end_column": li["column_number"],
                            # PEP-8: errors; all the rest: warnings
                            "annotation_level": "failure" if li["code"][0] in "EW" else "warning",
                            # "title": "Flake8",
                            "message": f"{li['code']}: {li['text']}"
                        })
                else:
                    print("\n" + out.strip("\n") + "\n")
                bad[fn] = bad.get(fn, []) + [linter_name]

    rmtree(tempdir)  # cleanup

    if bad:
        print("\nProblems found in the following files:")
        for fn, linters in bad.items():
            print(f"    {fn} ({', '.join(linters)})")
        print()
        if checks_api:
            annotate_check("flake8", False, annotations)
            print(json.dumps(annotations, indent=4))
        return 1
    elif checks_api:
        annotate_check("flake8", True, [])

    print("\nEvery file linted successfully")
    return 0


def annotate_check(check_name, success, annotations):
    """Use the GitHub Checks API to add annotations for the given check.

    A new check is created. We respect the limit of 50 annotations per call. Use `success` to
    set the status to successful, otherwise it will be marked as failed.

    Note that PyGithub does not support the Checks API, we therefore use requests.
    """
    sha = GH_EVENT()["pull_request"]["head"]["sha"]
    repo_url = GH_EVENT()["pull_request"]["head"]["repo"]["url"]
    check_url = f"{repo_url}/check-runs"

    post_data = {
        "name": "flake8",
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success" if success else "failure",
        "output": {
            # https://developer.github.com/v3/checks/runs/#output-object
            "title": "Flake8",
            "summary": "Python code linted using [Flake8](http://flake8.pycqa.org/).",
            # "text": "details here",
            "annotations": annotations
        }
    }

    resp = GH_REQ.post(check_url, post_data)
    print(resp.text)
    resp.raise_for_status()


if __name__ == "__main__":
    sys.exit(getattr(sys.modules[__name__], sys.argv[1])())
