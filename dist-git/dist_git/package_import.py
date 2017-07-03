# coding: utf-8

import logging
import os
import shutil
import types
import grp
import subprocess
import tempfile
import re
import munch
import multiprocessing

# pyrpkg uses os.getlogin(). It requires tty which is unavailable when we run this script as a daemon
# very dirty solution for now
import pwd

os.getlogin = lambda: pwd.getpwuid(os.getuid())[0]
# monkey patch end

from pyrpkg import Commands
from pyrpkg.errors import rpkgError

from providers import PackageContent
from exceptions import PackageImportException

import helpers

log = logging.getLogger(__name__)

import_lock = multiprocessing.Lock()

def my_upload_fabric(opts):
    def my_upload(repo_dir, reponame, abs_filename, filehash):
        """
        This is a replacement function for uploading sources.
        Rpkg uses upload.cgi for uploading which doesn't make sense
        on the local machine.
        """
        filename = os.path.basename(abs_filename)
        destination = os.path.join(opts.lookaside_location, reponame,
                                   filename, filehash, filename)

        # hack to allow "uploading" into lookaside
        current_gid = os.getgid()
        apache_gid = grp.getgrnam("apache").gr_gid
        os.setgid(apache_gid)

        if not os.path.isdir(os.path.dirname(destination)):
            try:
                os.makedirs(os.path.dirname(destination))
            except OSError as e:
                log.exception(str(e))

        if not os.path.exists(destination):
            shutil.copyfile(abs_filename, destination)

        os.setgid(current_gid)

    return my_upload


def sync_branch(new_branch, branch_commits, message):
    """
    Reset the 'new_branch' contents to contents of all branches in
    already in 'branch_commits.  But if possible, try to fast-forward merge
    only to minimize the git payload and to keep the git history as flatten
    as possible across all branches. Before calling this method, ensure that
    you are in the git directory and the 'new_branch' is checked out.
    """
    for branch in branch_commits:
        # Try to fast-forward merge against any other already pushed branch.
        # Note that if the branch is already there then merge request is no-op.
        if not subprocess.call(['git', 'merge', branch, '--ff-only']):
            log.debug("merged '{0}' fast forward into '{1}' or noop".format(branch, new_branch))
            return

    # No --fast-forward merge possible -> reset to the first available one.
    branch = next(iter(branch_commits))
    log.debug("resetting branch '{0}' to contents of '{1}'".format(new_branch, branch))
    subprocess.check_call(['git', 'read-tree', '-m', '-u', branch])

    # Get the AuthorDate from the original commit, to have consistent feeling.
    date = subprocess.check_output(['git', 'show', branch, '-q', '--format=%ai'])

    if subprocess.call(['git', 'diff', '--cached', '--exit-code']):
        # There's something to commit.
        subprocess.check_call(['git', 'commit', '--no-verify', '-m', message,
            '--date', date])
    else:
        log.debug("nothing to commit into branch '{0}'".format(new_branch))


def refresh_cgit_listing(opts):
    """
    Refresh cgit repository list. See cgit docs for more information.
    """
    try:
        cmd = ["/usr/share/copr/dist_git/bin/cgit_pkg_list", opts.cgit_pkg_list_location]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        log.error("cmd: {}, rc: {}, msg: {}".format(cmd, e.returncode, e.output.strip()))


def setup_git_repo(reponame, branches):
    """
    Invoke DistGit repo setup procedures.

    :param str reponame: name of the repository to be created
    :param str branches: branch names to be created inside that repo
    """
    log.info("make sure repos exist: {}".format(reponame))
    try:
        cmd = ["/usr/share/dist-git/setup_git_package", reponame]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        log.error("cmd: {}, rc: {}, msg: {}"
                  .format(cmd, e.returncode, e.output.strip()))
        if e.returncode == 128:
            log.info("Package already exists...continuing")
        else:
            raise PackageImportException(e.output)

    for branch in branches:
        try:
            cmd = ["/usr/share/dist-git/mkbranch", branch, reponame]
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            log.error("cmd: {}, rc: {}, msg: {}"
                      .format(cmd, e.returncode, e.output.strip()))
            if e.returncode == 128:
                log.info("Branch already exists...continuing")
            else:
                raise PackageImportException(e.output)


def import_package(opts, namespace, branches, package_content):
    """
    Import package into a DistGit repo for the given branches.

    :param Bunch opts: service configuration
    :param str namespace: repo name prefix
    :param list(str) branches: list of branch names to import into
    :param PackageContent package_content: all the package content

    :return Munch: resulting import data (pkg_info, branch_commits, reponame)
    """
    # lock here is for the case when parallel imports run for the same project
    if not import_lock.acquire(timeout=120):
        raise PackageImportException('import_package: lock could not be acquired.')

    log.debug("package_content: " + str(package_content))
    pkg_info = helpers.get_pkg_info(package_content.spec_path)
    log.debug("pkg_info: " + str(pkg_info))

    if not pkg_info.name:
        raise PackageImportException('Could not determine package name.')

    reponame = "{}/{}".format(namespace, pkg_info.name)
    setup_git_repo(reponame, branches)

    repo_dir = tempfile.mkdtemp()
    log.debug("repo_dir: {}".format(repo_dir))

    # use rpkg lib to import the source rpm
    commands = Commands(path=repo_dir,
                        lookaside="",
                        lookasidehash="md5",
                        lookaside_cgi="",
                        gitbaseurl=opts.git_base_url,
                        anongiturl="",
                        branchre="",
                        kojiconfig="",
                        build_client="")

    # rpkg gets module_name as a basename of git url
    # we use module_name as "username/projectname/package_name"
    # basename is not working here - so I'm setting it manually
    commands.module_name = reponame

    # rpkg calls upload.cgi script on the dist git server
    # here, I just copy the source files manually with custom function
    # I also add one parameter "repo_dir" to that function with this hack
    commands.lookasidecache.upload = types.MethodType(my_upload_fabric(opts), repo_dir)

    try:
        log.debug("clone the pkg repository into repo_dir directory")
        commands.clone(reponame, target=repo_dir)
    except Exception as e:
        log.error("Failed to clone the Git repository and add files.")
        raise PackageImportException(str(e))

    oldpath = os.getcwd()
    log.debug("Switching to repo_dir: {}".format(repo_dir))
    os.chdir(repo_dir)

    log.debug("Setting up Git user name and email.")
    helpers.run_cmd(['git', 'config', 'user.name', opts.git_user_name])
    helpers.run_cmd(['git', 'config', 'user.email', opts.git_user_email])

    message = "automatic import of {}".format(pkg_info.envr)
    branch_commits = {}
    for branch in branches:
        log.debug("checkout '{0}' branch".format(branch))
        commands.switch_branch(branch)

        try:
            if not branch_commits:
                log.debug("add package content")
                add_to_index = []
                shutil.copy(package_content.spec_path, repo_dir)
                add_to_index = [os.path.basename(package_content.spec_path)]

                for path in package_content.extra_content:
                    if os.path.isfile(path):
                        shutil.copy(path, repo_dir)
                    else:
                        shutil.copytree(path, repo_dir)
                    add_to_index.append(os.path.basename(path))

                commands.repo.index.add(add_to_index)

                log.debug("save the source files into lookaside cache")
                commands.upload(package_content.source_paths, replace=True)

                try:
                    log.debug("commit")
                    commands.commit(message)
                except rpkgError as e:
                    # Probably nothing to be committed.
                    log.error(str(e))
            else:
                sync_branch(branch, branch_commits, message)
        except:
            log.exception("Error during source uploading, merge, or commit.")
            continue

        try:
            log.debug("push")
            commands.push()
        except rpkgError as e:
            log.exception("Exception raised during push.")
            continue

        commands.load_commit()
        branch_commits[branch] = commands.commithash

    os.chdir(oldpath)
    shutil.rmtree(repo_dir)
    refresh_cgit_listing(opts)
    import_lock.release()

    return munch.Munch(
        pkg_info=pkg_info,
        branch_commits=branch_commits,
        reponame=reponame
    )
