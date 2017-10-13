import copy
import datetime
import json
import os
import flask
import json
import base64
import modulemd

from sqlalchemy.ext.associationproxy import association_proxy
from six.moves.urllib.parse import urljoin
from libravatar import libravatar_url
import zlib

from coprs import constants
from coprs import db
from coprs import helpers
from coprs import app

import itertools
import operator
from coprs.helpers import BuildSourceEnum, StatusEnum, ActionTypeEnum, JSONEncodedDict


class CoprSearchRelatedData(object):
    def get_search_related_copr_id(self):
        raise "Not Implemented"


class User(db.Model, helpers.Serializer):

    """
    Represents user of the copr frontend
    """

    # PK;  TODO: the 'username' could be also PK
    id = db.Column(db.Integer, primary_key=True)

    # unique username
    username = db.Column(db.String(100), nullable=False, unique=True)

    # email
    mail = db.Column(db.String(150), nullable=False)

    # optional timezone
    timezone = db.Column(db.String(50), nullable=True)

    # is this user proven? proven users can modify builder memory and
    # timeout for single builds
    proven = db.Column(db.Boolean, default=False)

    # is this user admin of the system?
    admin = db.Column(db.Boolean, default=False)

    # can this user behave as someone else?
    proxy = db.Column(db.Boolean, default=False)

    # stuff for the cli interface
    api_login = db.Column(db.String(40), nullable=False, default="abc")
    api_token = db.Column(db.String(40), nullable=False, default="abc")
    api_token_expiration = db.Column(
        db.Date, nullable=False, default=datetime.date(2000, 1, 1))

    # list of groups as retrieved from openid
    openid_groups = db.Column(JSONEncodedDict)

    @property
    def name(self):
        """
        Return the short username of the user, e.g. bkabrda
        """

        return self.username

    def permissions_for_copr(self, copr):
        """
        Get permissions of this user for the given copr.
        Caches the permission during one request,
        so use this if you access them multiple times
        """

        if not hasattr(self, "_permissions_for_copr"):
            self._permissions_for_copr = {}
        if copr.name not in self._permissions_for_copr:
            self._permissions_for_copr[copr.name] = (
                CoprPermission.query
                .filter_by(user=self)
                .filter_by(copr=copr)
                .first()
            )
        return self._permissions_for_copr[copr.name]

    def can_build_in(self, copr):
        """
        Determine if this user can build in the given copr.
        """
        can_build = False
        if copr.user_id == self.id:
            can_build = True
        if (self.permissions_for_copr(copr) and
                self.permissions_for_copr(copr).copr_builder ==
                helpers.PermissionEnum("approved")):

            can_build = True

        # a bit dirty code, here we access flask.session object
        if copr.group is not None and \
                copr.group.fas_name in self.user_teams:
            return True

        return can_build

    @property
    def user_teams(self):
        if self.openid_groups and 'fas_groups' in self.openid_groups:
            return self.openid_groups['fas_groups']
        else:
            return []

    @property
    def user_groups(self):
        return Group.query.filter(Group.fas_name.in_(self.user_teams)).all()

    def can_build_in_group(self, group):
        """
        :type group: Group
        """
        if group.fas_name in self.user_teams:
            return True
        else:
            return False

    def can_edit(self, copr):
        """
        Determine if this user can edit the given copr.
        """

        if copr.user == self or self.admin:
            return True
        if (self.permissions_for_copr(copr) and
                self.permissions_for_copr(copr).copr_admin ==
                helpers.PermissionEnum("approved")):

            return True

        if copr.group is not None and \
                copr.group.fas_name in self.user_teams:
            return True

        return False

    @property
    def serializable_attributes(self):
        # enumerate here to prevent exposing credentials
        return ["id", "name"]

    @property
    def coprs_count(self):
        """
        Get number of coprs for this user.
        """

        return (Copr.query.filter_by(user=self).
                filter_by(deleted=False).
                filter_by(group_id=None).
                count())

    @property
    def gravatar_url(self):
        """
        Return url to libravatar image.
        """

        try:
            return libravatar_url(email=self.mail, https=True)
        except IOError:
            return ""


class Copr(db.Model, helpers.Serializer, CoprSearchRelatedData):

    """
    Represents a single copr (private repo with builds, mock chroots, etc.).
    """

    id = db.Column(db.Integer, primary_key=True)
    # name of the copr, no fancy chars (checked by forms)
    name = db.Column(db.String(100), nullable=False)
    homepage = db.Column(db.Text)
    contact = db.Column(db.Text)
    # string containing urls of additional repos (separated by space)
    # that this copr will pull dependencies from
    repos = db.Column(db.Text)
    # time of creation as returned by int(time.time())
    created_on = db.Column(db.Integer)
    # description and instructions given by copr owner
    description = db.Column(db.Text)
    instructions = db.Column(db.Text)
    deleted = db.Column(db.Boolean, default=False)
    playground = db.Column(db.Boolean, default=False)

    # should copr run `createrepo` each time when build packages are changed
    auto_createrepo = db.Column(db.Boolean, default=True)

    # relations
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship("User", backref=db.backref("coprs"))
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    group = db.relationship("Group", backref=db.backref("groups"))
    mock_chroots = association_proxy("copr_chroots", "mock_chroot")
    forked_from_id = db.Column(db.Integer, db.ForeignKey("copr.id"))
    forked_from = db.relationship("Copr", remote_side=id, backref=db.backref("forks"))

    # a secret to be used for webhooks authentication
    webhook_secret = db.Column(db.String(100))

    # enable networking for the builds by default
    build_enable_net = db.Column(db.Boolean, default=True,
                                 server_default="1", nullable=False)

    unlisted_on_hp = db.Column(db.Boolean, default=False, nullable=False)

    # information for search index updating
    latest_indexed_data_update = db.Column(db.Integer)

    # builds and the project are immune against deletion
    persistent = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # if backend deletion script should be run for the project's builds
    auto_prune = db.Column(db.Boolean, default=True, nullable=False, server_default="1")

    # use mock's bootstrap container feature
    use_bootstrap_container = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    # if chroots for the new branch should be auto-enabled and populated from rawhide ones
    follow_fedora_branching = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    __mapper_args__ = {
        "order_by": created_on.desc()
    }

    @property
    def is_a_group_project(self):
        """
        Return True if copr belongs to a group
        """
        return self.group_id is not None

    @property
    def owner(self):
        """
        Return owner (user or group) of this copr
        """
        return self.group if self.is_a_group_project else self.user

    @property
    def owner_name(self):
        """
        Return @group.name for a copr owned by a group and user.name otherwise
        """
        return self.group.at_name if self.is_a_group_project else self.user.name

    @property
    def repos_list(self):
        """
        Return repos of this copr as a list of strings
        """
        return self.repos.split()

    @property
    def active_chroots(self):
        """
        Return list of active mock_chroots of this copr
        """

        return filter(lambda x: x.is_active, self.mock_chroots)

    @property
    def active_copr_chroots(self):
        """
        :rtype: list of CoprChroot
        """
        return [c for c in self.copr_chroots if c.is_active]

    @property
    def active_chroots_sorted(self):
        """
        Return list of active mock_chroots of this copr
        """

        return sorted(self.active_chroots, key=lambda ch: ch.name)

    @property
    def active_chroots_grouped(self):
        """
        Return list of active mock_chroots of this copr
        """

        chroots = [("{} {}".format(c.os_release, c.os_version), c.arch) for c in self.active_chroots_sorted]
        output = []
        for os, chs in itertools.groupby(chroots, operator.itemgetter(0)):
            output.append((os, [ch[1] for ch in chs]))

        return output

    @property
    def build_count(self):
        """
        Return number of builds in this copr
        """

        return len(self.builds)

    @property
    def disable_createrepo(self):

        return not self.auto_createrepo

    @disable_createrepo.setter
    def disable_createrepo(self, value):

        self.auto_createrepo = not bool(value)

    @property
    def modified_chroots(self):
        """
        Return list of chroots which has been modified
        """
        modified_chroots = []
        for chroot in self.copr_chroots:
            if ((chroot.buildroot_pkgs or chroot.repos)
                    and chroot.is_active):
                modified_chroots.append(chroot)
        return modified_chroots

    def is_release_arch_modified(self, name_release, arch):
        if "{}-{}".format(name_release, arch) in \
                [chroot.name for chroot in self.modified_chroots]:
            return True
        return False

    @property
    def full_name(self):
        return "{}/{}".format(self.owner_name, self.name)

    @property
    def repo_name(self):
        return "{}-{}".format(self.owner_name, self.name)

    @property
    def repo_url(self):
        return "/".join([app.config["BACKEND_BASE_URL"],
                         u"results",
                         self.full_name])

    @property
    def repo_id(self):
        if self.is_a_group_project:
            return "group_{}-{}".format(self.group.name, self.name)
        else:
            return "{}-{}".format(self.user.name, self.name)

    @property
    def modules_url(self):
        return "/".join([self.repo_url, "modules"])

    def to_dict(self, private=False, show_builds=True, show_chroots=True):
        result = {}
        for key in ["id", "name", "description", "instructions"]:
            result[key] = str(copy.copy(getattr(self, key)))
        result["owner"] = self.owner_name
        return result

    @property
    def still_forking(self):
        return bool(Action.query.filter(Action.result == helpers.BackendResultEnum("waiting"))
                    .filter(Action.action_type == helpers.ActionTypeEnum("fork"))
                    .filter(Action.new_value == self.full_name).all())

    def get_search_related_copr_id(self):
        return self.id


class CoprPermission(db.Model, helpers.Serializer):

    """
    Association class for Copr<->Permission relation
    """

    # see helpers.PermissionEnum for possible values of the fields below
    # can this user build in the copr?
    copr_builder = db.Column(db.SmallInteger, default=0)
    # can this user serve as an admin? (-> edit and approve permissions)
    copr_admin = db.Column(db.SmallInteger, default=0)

    # relations
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    user = db.relationship("User", backref=db.backref("copr_permissions"))
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"), primary_key=True)
    copr = db.relationship("Copr", backref=db.backref("copr_permissions"))


class Package(db.Model, helpers.Serializer, CoprSearchRelatedData):
    """
    Represents a single package in a project.
    """
    __table_args__ = (
        db.UniqueConstraint('copr_id', 'name', name='packages_copr_pkgname'),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    # Source of the build: type identifier
    source_type = db.Column(db.Integer, default=helpers.BuildSourceEnum("unset"))
    # Source of the build: description in json, example: git link, srpm url, etc.
    source_json = db.Column(db.Text)
    # True if the package is built automatically via webhooks
    webhook_rebuild = db.Column(db.Boolean, default=False)
    # enable networking during a build process
    enable_net = db.Column(db.Boolean, default=False,
                           server_default="0", nullable=False)

    # @TODO Remove me few weeks after Copr migration
    # Contain status of the Package before migration
    # Normally the `status` is not stored in `Package`. It is computed from `status` variable of `BuildChroot`,
    # but `old_status` has to be stored here, because we migrate whole `package` table, but only succeeded builds.
    # Therefore if `old_status` was in `BuildChroot` we wouldn't be able to know old state of non-succeeded packages
    # even though it would be known before migration.
    old_status = db.Column(db.Integer)

    builds = db.relationship("Build", order_by="Build.id")

    # relations
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"))
    copr = db.relationship("Copr", backref=db.backref("packages"))

    @property
    def dist_git_repo(self):
        return "{}/{}".format(self.copr.full_name, self.name)

    @property
    def source_json_dict(self):
        if not self.source_json:
            return {}
        return json.loads(self.source_json)

    @property
    def source_type_text(self):
        return helpers.BuildSourceEnum(self.source_type)

    @property
    def has_source_type_set(self):
        """
        Package's source type (and source_json) is being derived from its first build, which works except
        for "link" and "upload" cases. Consider these being equivalent to source_type being unset.
        """
        return self.source_type and self.source_type_text != "link" and self.source_type_text != "upload"

    @property
    def dist_git_url(self):
        if "DIST_GIT_URL" in app.config:
            return "{}/{}.git".format(app.config["DIST_GIT_URL"], self.dist_git_repo)
        return None

    @property
    def dist_git_clone_url(self):
        if "DIST_GIT_CLONE_URL" in app.config:
            return "{}/{}.git".format(app.config["DIST_GIT_CLONE_URL"], self.dist_git_repo)
        else:
            return self.dist_git_url

    def last_build(self, successful=False):
        for build in reversed(self.builds):
            if not successful or build.state == "succeeded":
                return build
        return None

    def to_dict(self, with_latest_build=False, with_latest_succeeded_build=False, with_all_builds=False):
        package_dict = super(Package, self).to_dict()
        package_dict['source_type'] = helpers.BuildSourceEnum(package_dict['source_type'])

        if with_latest_build:
            build = self.last_build(successful=False)
            package_dict['latest_build'] = build.to_dict(with_chroot_states=True) if build else None
        if with_latest_succeeded_build:
            build = self.last_build(successful=True)
            package_dict['latest_succeeded_build'] = build.to_dict(with_chroot_states=True) if build else None
        if with_all_builds:
            package_dict['builds'] = [build.to_dict(with_chroot_states=True) for build in reversed(self.builds)]

        return package_dict

    def get_search_related_copr_id(self):
        return self.copr.id


class Build(db.Model, helpers.Serializer):
    """
    Representation of one build in one copr
    """
    __table_args__ = (db.Index('build_canceled', "canceled"), )

    id = db.Column(db.Integer, primary_key=True)
    # single url to the source rpm, should not contain " ", "\n", "\t"
    pkgs = db.Column(db.Text)
    # built packages
    built_packages = db.Column(db.Text)
    # version of the srpm package got by rpm
    pkg_version = db.Column(db.Text)
    # was this build canceled by user?
    canceled = db.Column(db.Boolean, default=False)
    # list of space separated additional repos
    repos = db.Column(db.Text)
    # the three below represent time of important events for this build
    # as returned by int(time.time())
    submitted_on = db.Column(db.Integer, nullable=False)
    # url of the build results
    results = db.Column(db.Text)
    # memory requirements for backend builder
    memory_reqs = db.Column(db.Integer, default=constants.DEFAULT_BUILD_MEMORY)
    # maximum allowed time of build, build will fail if exceeded
    timeout = db.Column(db.Integer, default=constants.DEFAULT_BUILD_TIMEOUT)
    # enable networking during a build process
    enable_net = db.Column(db.Boolean, default=False,
                           server_default="0", nullable=False)
    # Source of the build: type identifier
    source_type = db.Column(db.Integer, default=helpers.BuildSourceEnum("unset"))
    # Source of the build: description in json, example: git link, srpm url, etc.
    source_json = db.Column(db.Text)
    # Type of failure: type identifier
    fail_type = db.Column(db.Integer, default=helpers.FailTypeEnum("unset"))
    # background builds has lesser priority than regular builds.
    is_background = db.Column(db.Boolean, default=False, server_default="0", nullable=False)

    srpm_url = db.Column(db.Text)

    # relations
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship("User", backref=db.backref("builds"))
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"))
    copr = db.relationship("Copr", backref=db.backref("builds"))
    package_id = db.Column(db.Integer, db.ForeignKey("package.id"))
    package = db.relationship("Package")

    chroots = association_proxy("build_chroots", "mock_chroot")

    batch_id = db.Column(db.Integer, db.ForeignKey("batch.id"))
    batch = db.relationship("Batch", backref=db.backref("builds"))

    @property
    def user_name(self):
        return self.user.name

    @property
    def group_name(self):
        return self.copr.group.name

    @property
    def copr_name(self):
        return self.copr.name

    @property
    def fail_type_text(self):
        return helpers.FailTypeEnum(self.fail_type)

    @property
    def is_older_results_naming_used(self):
        # we have changed result directory naming together with transition to dist-git
        # that's why we use so strange criterion
        return self.build_chroots[0].git_hash is None

    @property
    def repos_list(self):
        if self.repos is None:
            return list()
        else:
            return self.repos.split()

    @property
    def import_task_id(self):
        return str(self.id)

    @property
    def id_fixed_width(self):
        return "{:08d}".format(self.id)

    @property
    def import_log_urls(self):
        backend_log = self.import_log_url_backend
        types = [helpers.BuildSourceEnum("upload"), helpers.BuildSourceEnum("link")]
        if self.source_type in types:
            if json.loads(self.source_json).get("url", "").endswith(".src.rpm"):
                backend_log = None
        return filter(None, [backend_log, self.import_log_url_distgit])

    @property
    def import_log_url_distgit(self):
        if app.config["COPR_DIST_GIT_LOGS_URL"]:
            return "{}/{}.log".format(app.config["COPR_DIST_GIT_LOGS_URL"],
                                      self.import_task_id.replace('/', '_'))
        return None

    @property
    def import_log_url_backend(self):
        parts = ["results", self.copr.owner_name, self.copr.name,
                 "srpm-builds", self.id_fixed_width, "builder-live.log"]
        path = os.path.normpath(os.path.join(*parts))
        return urljoin(app.config["BACKEND_BASE_URL"], path)

    @property
    def result_dir_name(self):
        # We can remove this ugly condition after migrating Copr to new machines
        # It is throw-back from era before dist-git
        if self.is_older_results_naming_used:
            return self.src_pkg_name
        return "-".join([self.id_fixed_width, self.package.name])

    @property
    def source_json_dict(self):
        if not self.source_json:
            return {}
        return json.loads(self.source_json)

    @property
    def started_on(self):
        return self.min_started_on

    @property
    def min_started_on(self):
        mb_list = [chroot.started_on for chroot in
                   self.build_chroots if chroot.started_on]
        if len(mb_list) > 0:
            return min(mb_list)
        else:
            return None

    @property
    def ended_on(self):
        return self.max_ended_on

    @property
    def max_ended_on(self):
        if not self.build_chroots:
            return None
        if any(chroot.ended_on is None for chroot in self.build_chroots):
            return None
        return max(chroot.ended_on for chroot in self.build_chroots)

    @property
    def chroots_started_on(self):
        return {chroot.name: chroot.started_on for chroot in self.build_chroots}

    @property
    def chroots_ended_on(self):
        return {chroot.name: chroot.ended_on for chroot in self.build_chroots}

    @property
    def source_type_text(self):
        return helpers.BuildSourceEnum(self.source_type)

    @property
    def source_metadata(self):
        if self.source_json is None:
            return None

        try:
            return json.loads(self.source_json)
        except (TypeError, ValueError):
            return None

    @property
    def chroot_states(self):
        return map(lambda chroot: chroot.status, self.build_chroots)

    def get_chroots_by_status(self, statuses=None):
        """
        Get build chroots with states which present in `states` list
        If states == None, function returns build_chroots
        """
        chroot_states_map = dict(zip(self.build_chroots, self.chroot_states))
        if statuses is not None:
            statuses = set(statuses)
        else:
            return self.build_chroots

        return [
            chroot for chroot, status in chroot_states_map.items()
            if status in statuses
        ]

    @property
    def chroots_dict_by_name(self):
        return {b.name: b for b in self.build_chroots}

    @property
    def has_pending_chroot(self):
        # FIXME bad name
        # used when checking if the repo is initialized and results can be set
        # i think this is the only purpose - check
        return StatusEnum("pending") in self.chroot_states or \
            StatusEnum("starting") in self.chroot_states

    @property
    def has_unfinished_chroot(self):
        return StatusEnum("pending") in self.chroot_states or \
            StatusEnum("starting") in self.chroot_states or \
            StatusEnum("running") in self.chroot_states

    @property
    def has_importing_chroot(self):
        return StatusEnum("importing") in self.chroot_states

    @property
    def status(self):
        """
        Return build status according to build status of its chroots
        """
        if self.canceled:
            return StatusEnum("canceled")

        for state in ["running", "starting", "importing", "pending", "failed", "succeeded", "skipped", "forked"]:
            if StatusEnum(state) in self.chroot_states:
                return StatusEnum(state)

    @property
    def state(self):
        """
        Return text representation of status of this build
        """

        if self.status is not None:
            return StatusEnum(self.status)

        return "unknown"

    @property
    def cancelable(self):
        """
        Find out if this build is cancelable.

        Build is cancelabel only when it's pending (not started)
        """

        return self.status == StatusEnum("pending") or \
            self.status == StatusEnum("importing") or \
            self.status == StatusEnum("running")

    @property
    def repeatable(self):
        """
        Find out if this build is repeatable.

        Build is repeatable only if it's not pending, starting or running
        """
        return self.status not in [StatusEnum("pending"),
                                   StatusEnum("starting"),
                                   StatusEnum("running"),
                                   StatusEnum("forked")]

    @property
    def finished(self):
        """
        Find out if this build is in finished state.

        Build is finished only if all its build_chroots are in finished state.
        """
        return all([(chroot.state in ["succeeded", "forked", "canceled", "skipped", "failed"]) for chroot in self.build_chroots])

    @property
    def persistent(self):
        """
        Find out if this build is persistent.

        This property is inherited from the project.
        """
        return self.copr.persistent

    @property
    def src_pkg_name(self):
        """
        Extract source package name from source name or url
        todo: obsolete
        """
        try:
            src_rpm_name = self.pkgs.split("/")[-1]
        except:
            return None
        if src_rpm_name.endswith(".src.rpm"):
            return src_rpm_name[:-8]
        else:
            return src_rpm_name

    @property
    def package_name(self):
        try:
            return self.package.name
        except:
            return None

    def to_dict(self, options=None, with_chroot_states=False):
        result = super(Build, self).to_dict(options)
        result["src_pkg"] = result["pkgs"]
        del result["pkgs"]
        del result["copr_id"]

        result['source_type'] = helpers.BuildSourceEnum(result['source_type'])
        result["state"] = self.state

        if with_chroot_states:
            result["chroots"] = {b.name: b.state for b in self.build_chroots}

        return result


class DistGitBranch(db.Model, helpers.Serializer):
    """
    1:N mapping: branch -> chroots
    """

    # Name of the branch used on dist-git machine.
    name = db.Column(db.String(50), primary_key=True)


class MockChroot(db.Model, helpers.Serializer):

    """
    Representation of mock chroot
    """
    __table_args__ = (
        db.UniqueConstraint('os_release', 'os_version', 'arch', name='mock_chroot_uniq'),
    )

    id = db.Column(db.Integer, primary_key=True)
    # fedora/epel/..., mandatory
    os_release = db.Column(db.String(50), nullable=False)
    # 18/rawhide/..., optional (mock chroot doesn"t need to have this)
    os_version = db.Column(db.String(50), nullable=False)
    # x86_64/i686/..., mandatory
    arch = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=True)

    # Reference branch name
    distgit_branch_name  = db.Column(db.String(50),
                                     db.ForeignKey("dist_git_branch.name"),
                                     nullable=False)

    distgit_branch = db.relationship("DistGitBranch",
            backref=db.backref("chroots"))

    @property
    def name(self):
        """
        Textual representation of name of this chroot
        """
        return "{}-{}-{}".format(self.os_release, self.os_version, self.arch)

    @property
    def name_release(self):
        """
        Textual representation of name of this or release
        """
        return "{}-{}".format(self.os_release, self.os_version)

    @property
    def name_release_human(self):
        """
        Textual representation of name of this or release
        """
        return "{} {}".format(self.os_release, self.os_version)

    @property
    def os(self):
        """
        Textual representation of the operating system name
        """
        return "{0} {1}".format(self.os_release, self.os_version)

    @property
    def serializable_attributes(self):
        attr_list = super(MockChroot, self).serializable_attributes
        attr_list.extend(["name", "os"])
        return attr_list


class CoprChroot(db.Model, helpers.Serializer):

    """
    Representation of Copr<->MockChroot relation
    """

    buildroot_pkgs = db.Column(db.Text)
    repos = db.Column(db.Text, default="", server_default="", nullable=False)
    mock_chroot_id = db.Column(
        db.Integer, db.ForeignKey("mock_chroot.id"), primary_key=True)
    mock_chroot = db.relationship(
        "MockChroot", backref=db.backref("copr_chroots"))
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"), primary_key=True)
    copr = db.relationship("Copr",
                           backref=db.backref(
                               "copr_chroots",
                               single_parent=True,
                               cascade="all,delete,delete-orphan"))

    comps_zlib = db.Column(db.LargeBinary(), nullable=True)
    comps_name = db.Column(db.String(127), nullable=True)

    module_md_zlib = db.Column(db.LargeBinary(), nullable=True)
    module_md_name = db.Column(db.String(127), nullable=True)

    def update_comps(self, comps_xml):
        self.comps_zlib = zlib.compress(comps_xml.encode("utf-8"))

    def update_module_md(self, module_md_yaml):
        self.module_md_zlib = zlib.compress(module_md_yaml.encode("utf-8"))

    @property
    def buildroot_pkgs_list(self):
        return self.buildroot_pkgs.split()

    @property
    def repos_list(self):
        return self.repos.split()

    @property
    def comps(self):
        if self.comps_zlib:
            return zlib.decompress(self.comps_zlib).decode("utf-8")

    @property
    def module_md(self):
        if self.module_md_zlib:
            return zlib.decompress(self.module_md_zlib).decode("utf-8")

    @property
    def comps_len(self):
        if self.comps_zlib:
            return len(zlib.decompress(self.comps_zlib))
        else:
            return 0

    @property
    def module_md_len(self):
        if self.module_md_zlib:
            return len(zlib.decompress(self.module_md_zlib))
        else:
            return 0

    @property
    def name(self):
        return self.mock_chroot.name

    @property
    def is_active(self):
        return self.mock_chroot.is_active

    def to_dict(self):
        options = {"__columns_only__": [
            "buildroot_pkgs", "repos", "comps_name", "copr_id"
        ]}
        d = super(CoprChroot, self).to_dict(options=options)
        d["mock_chroot"] = self.mock_chroot.name
        return d


class BuildChroot(db.Model, helpers.Serializer):

    """
    Representation of Build<->MockChroot relation
    """

    mock_chroot_id = db.Column(db.Integer, db.ForeignKey("mock_chroot.id"),
                               primary_key=True)
    mock_chroot = db.relationship("MockChroot", backref=db.backref("builds"))
    build_id = db.Column(db.Integer, db.ForeignKey("build.id"),
                         primary_key=True)
    build = db.relationship("Build", backref=db.backref("build_chroots"))
    git_hash = db.Column(db.String(40))
    status = db.Column(db.Integer, default=StatusEnum("importing"))

    started_on = db.Column(db.Integer)
    ended_on = db.Column(db.Integer, index=True)

    last_deferred = db.Column(db.Integer)

    build_requires = db.Column(db.Text)

    @property
    def name(self):
        """
        Textual representation of name of this chroot
        """

        return self.mock_chroot.name

    @property
    def state(self):
        """
        Return text representation of status of this build chroot
        """

        if self.status is not None:
            return StatusEnum(self.status)

        return "unknown"

    @property
    def task_id(self):
        return "{}-{}".format(self.build_id, self.name)

    @property
    def dist_git_url(self):
        if app.config["DIST_GIT_URL"]:
            if self.state == "forked":
                coprname = self.build.copr.forked_from.full_name
            else:
                coprname = self.build.copr.full_name
            return "{}/{}/{}.git/commit/?id={}".format(app.config["DIST_GIT_URL"],
                                                coprname,
                                                self.build.package.name,
                                                self.git_hash)
        return None

    @property
    def result_dir_url(self):
        return urljoin(app.config["BACKEND_BASE_URL"],
                       os.path.join("results", self.result_dir, "")
                      )

    @property
    def result_dir(self):
        # hide changes occurred after migration to dist-git
        # if build has defined dist-git, it means that new schema should be used
        # otherwise use older structure

        # old: results/valtri/ruby/fedora-rawhide-x86_64/rubygem-aws-sdk-resources-2.1.11-1.fc24/
        # new: results/asamalik/rh-perl520/epel-7-x86_64/00000187-rh-perl520/

        parts = [self.build.copr.owner_name]

        parts.extend([
            self.build.copr.name,
            self.name,
        ])
        if self.git_hash is not None and self.build.package:
            parts.append(self.build.result_dir_name)
        else:
            parts.append(self.build.src_pkg_name)

        return os.path.join(*parts)

    def __str__(self):
        return "<BuildChroot: {}>".format(self.to_dict())


class LegalFlag(db.Model, helpers.Serializer):
    id = db.Column(db.Integer, primary_key=True)
    # message from user who raised the flag (what he thinks is wrong)
    raise_message = db.Column(db.Text)
    # time of raising the flag as returned by int(time.time())
    raised_on = db.Column(db.Integer)
    # time of resolving the flag by admin as returned by int(time.time())
    resolved_on = db.Column(db.Integer)

    # relations
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"), nullable=True)
    # cascade="all" means that we want to keep these even if copr is deleted
    copr = db.relationship(
        "Copr", backref=db.backref("legal_flags", cascade="all"))
    # user who reported the problem
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reporter = db.relationship("User",
                               backref=db.backref("legal_flags_raised"),
                               foreign_keys=[reporter_id],
                               primaryjoin="LegalFlag.reporter_id==User.id")
    # admin who resolved the problem
    resolver_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=True)
    resolver = db.relationship("User",
                               backref=db.backref("legal_flags_resolved"),
                               foreign_keys=[resolver_id],
                               primaryjoin="LegalFlag.resolver_id==User.id")


class Action(db.Model, helpers.Serializer):

    """
    Representation of a custom action that needs
    backends cooperation/admin attention/...
    """

    id = db.Column(db.Integer, primary_key=True)
    # delete, rename, ...; see ActionTypeEnum
    action_type = db.Column(db.Integer, nullable=False)
    # copr, ...; downcase name of class of modified object
    object_type = db.Column(db.String(20))
    # id of the modified object
    object_id = db.Column(db.Integer)
    # old and new values of the changed property
    old_value = db.Column(db.String(255))
    new_value = db.Column(db.String(255))
    # additional data
    data = db.Column(db.Text)
    # result of the action, see helpers.BackendResultEnum
    result = db.Column(
        db.Integer, default=helpers.BackendResultEnum("waiting"))
    # optional message from the backend/whatever
    message = db.Column(db.Text)
    # time created as returned by int(time.time())
    created_on = db.Column(db.Integer)
    # time ended as returned by int(time.time())
    ended_on = db.Column(db.Integer)

    def __str__(self):
        return self.__unicode__()

    def __unicode__(self):
        if self.action_type == ActionTypeEnum("delete"):
            return "Deleting {0} {1}".format(self.object_type, self.old_value)
        elif self.action_type == ActionTypeEnum("rename"):
            return "Renaming {0} from {1} to {2}.".format(self.object_type,
                                                          self.old_value,
                                                          self.new_value)
        elif self.action_type == ActionTypeEnum("legal-flag"):
            return "Legal flag on copr {0}.".format(self.old_value)

        return "Action {0} on {1}, old value: {2}, new value: {3}.".format(
            self.action_type, self.object_type, self.old_value, self.new_value)

    def to_dict(self, **kwargs):
        d = super(Action, self).to_dict()
        if d.get("object_type") == "module":
            module = Module.query.filter(Module.id == d["object_id"]).first()
            data = json.loads(d["data"])
            data.update({
                "projectname": module.copr.name,
                "ownername": module.copr.owner_name,
                "modulemd_b64": module.yaml_b64,
            })
            d["data"] = json.dumps(data)
        return d


class Krb5Login(db.Model, helpers.Serializer):
    """
    Represents additional user information for kerberos authentication.
    """

    __tablename__ = "krb5_login"

    # FK to User table
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    # 'string' from 'copr.conf' from KRB5_LOGIN[string]
    config_name = db.Column(db.String(30), nullable=False, primary_key=True)

    # krb's primary, i.e. 'username' from 'username@EXAMPLE.COM'
    primary = db.Column(db.String(80), nullable=False, primary_key=True)

    user = db.relationship("User", backref=db.backref("krb5_logins"))


class CounterStat(db.Model, helpers.Serializer):
    """
    Generic store for simple statistics.
    """

    name = db.Column(db.String(127), primary_key=True)
    counter_type = db.Column(db.String(30))

    counter = db.Column(db.Integer, default=0, server_default="0")


class Group(db.Model, helpers.Serializer):
    """
    Represents FAS groups and their aliases in Copr
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(127))

    # TODO: add unique=True
    fas_name = db.Column(db.String(127))

    @property
    def at_name(self):
        return u"@{}".format(self.name)

    def __str__(self):
        return self.__unicode__()

    def __unicode__(self):
        return "{} (fas: {})".format(self.name, self.fas_name)


class Batch(db.Model):
    id = db.Column(db.Integer, primary_key=True)


class Module(db.Model, helpers.Serializer):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    stream = db.Column(db.String(100), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    summary = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    created_on = db.Column(db.Integer, nullable=True)

    # When someone submits YAML (not generate one on the copr modules page), we might want to use that exact file.
    # Yaml produced by deconstructing into pieces and constructed back can look differently,
    # which is not desirable (Imo)
    #
    # Also if there are fields which are not covered by this model, we will be able to add them in the future
    # and fill them with data from this blob
    yaml_b64 = db.Column(db.Text)

    # relations
    copr_id = db.Column(db.Integer, db.ForeignKey("copr.id"))
    copr = db.relationship("Copr", backref=db.backref("modules"))

    @property
    def yaml(self):
        return base64.b64decode(self.yaml_b64)

    @property
    def modulemd(self):
        mmd = modulemd.ModuleMetadata()
        mmd.loads(self.yaml)
        return mmd

    @property
    def nsv(self):
        return "-".join([self.name, self.stream, str(self.version)])

    @property
    def full_name(self):
        return "{}/{}".format(self.copr.full_name, self.nsv)

    @property
    def action(self):
        return Action.query.filter(Action.object_type == "module").filter(Action.object_id == self.id).first()

    @property
    def state(self):
        """
        Return text representation of status of this build
        """
        if self.action is not None:
            return helpers.ModuleStatusEnum(self.action.result)
        return "-"

    def repo_url(self, arch):
        # @TODO Use custom chroot instead of fedora-24
        # @TODO Get rid of OS name from module path, see how koji does it
        # https://kojipkgs.stg.fedoraproject.org/repos/module-base-runtime-0.25-9/latest/x86_64/toplink/packages/module-build-macros/0.1/
        module_dir = "fedora-24-{}+{}-{}-{}".format(arch, self.name, self.stream, self.version)
        return "/".join([self.copr.repo_url, "modules", module_dir, "latest", arch])
