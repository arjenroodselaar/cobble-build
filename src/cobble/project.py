"""Project-level configuration and state."""

import os.path

class Project(object):
    """A Project tracks the overall build configuration, filesystem paths,
    registered plugins/keys, etc. and provides services that relate to that."""

    def __init__(self, root, build_dir, alias):
        """Creates a Project.

        root: path to root of project structure.
        build_dir: path to build directory.
        alias: name/alias used for this project.
        """
        self.root = root
        self.build_dir = build_dir
        self.alias = alias

        self.subprojects = {}
        self.named_envs = {}
        self.packages = {}
        self.ninja_rules = {
            'cobble_symlink_product': {
                'command': 'ln -sf $target $out',
                'description': 'SYMLINK $out',
            },
        }

    # TODO: rename something like static_path?
    def inpath(self, *parts):
        """Creates a path to an input resource within the project tree by
        separating the given path components by the path separator
        character."""
        return os.path.join(self.root, *parts)

    def outpath(self, env, *parts):
        """Creates a path to an output resource within the build directory.

        Output resources are distinguished by their environments; the same
        product may be built several times, in different environments, and
        stored in separate places. Thus, 'outpath' requires the environment to
        be provided.
        """
        return os.path.join(self.build_dir, 'env', self.alias, env.digest, *parts)

    def linkpath(self, *parts):
        """Creates a path into the 'latest' symlinks in the build directory."""
        return os.path.join(self.build_dir, 'latest', self.alias, *parts)

    def add_subproject(self, subproject):
        """Registers a subproject 'project' with the project."""
        assert subproject.alias not in self.subprojects, \
            "Duplicate subproject %r" % subproject.alias
        self.subprojects[subproject.alias] = subproject

    def find_project(self, alias):
        if alias == '' or alias == self.alias:
            return self
        else:
            # Recursively ask each subproject if they know about this alias.
            # This may return multiple results, but the loader should guarantee
            # no two projects share the same alias.
            possible_projects = [
                p.find_project(alias) for p
                in self.subprojects.values()
            ]
            return next(filter(None, possible_projects), None)

    def add_package(self, package):
        """Registers 'package' with the project."""
        assert package.relpath not in self.packages, \
                "duplicate package at %s" % package.relpath
        assert package.project is self, "package project misconfigured"
        self.packages[package.relpath] = package

    def find_target(self, ident):
        """Finds the 'Target' named by an 'ident'.

        'find_target' at the 'Project' level requires absolute identifiers,
        e.g. '//foo/bar:target' or 'sub//foo/bar:target'.
        """
        assert '//' in ident, "Expected absolute identifier: %r" % ident
        alias, package_and_target = ident.split('//')

        if alias:
            project = self.find_project(alias)
            assert project, "No project with alias %r" % alias
            return project.find_target('//' + package_and_target)

        colons_in_remainder = package_and_target.count(':')
        if colons_in_remainder == 0:
            # Target name not specified
            pkg_path = package_and_target
            target_name = os.path.basename(pkg_path)
        elif colons_in_remainder == 1:
            # Explicit target name
            pkg_path, target_name = package_and_target.split(':')
        else:
            raise Exception('Too many colons in identifier: %r' % ident)

        assert pkg_path in self.packages, \
               "Reference to unknown package: %r" % ident
        assert target_name in self.packages[pkg_path].targets, \
                "Target %s not found in package %s" % \
                    (target_name, self.alias + '//' + pkg_path)
        return self.packages[pkg_path].targets[target_name]

    def define_environment(self, name, env):
        """Defines a named environment in the project.

        Named environments are defined in BUILD.conf, and provide the basis for
        all other environments.
        """
        assert name not in self.named_envs, \
            "more than one environment named %s" % name
        self.named_envs[name] = env

    def find_environment(self, ident):
        alias, name = ident.split('//') if '//' in ident else ('', ident)
        project = self.find_project(alias)
        assert project, "No project with alias %r" % alias
        assert name in project.named_envs, "No environment named %r" % ident
        return project.named_envs[name]

    def add_ninja_rules(self, rules):
        """Extends the set of Ninja rules used in the project.

        Ninja rules are represented as dicts with keys matching the attributes
        of Ninja's rule syntax.
        """
        for k, v in rules.items():
            if k in self.ninja_rules:
                assert v == self.ninja_rules[k], \
                        "ninja rule %s defined incompatibly in multiple places" % k
            else:
                self.ninja_rules[k] = v

    def files(self):
        """Returns an iterator over the build files and BUILD.conf."""
        yield self.inpath('BUILD.conf')
        for p in self.packages.values():
            yield p.inpath('BUILD')

    def targets(self):
        """Returns an iterator over all Targets in the project."""
        for p in self.packages.values():
            for t in p.targets.values():
                yield t

    def concrete_targets(self):
        """Returns an iterator over the concrete Targets in the project."""
        return filter(lambda t: t.concrete, self.targets())

class Package(object):
    def __init__(self, project, relpath):
        """Creates a Package and registers it with 'project'."""
        self.project = project
        self.relpath = os.path.normpath(relpath)
        self.targets = {}

        project.add_package(self)

    def add_target(self, target):
        """Adds a 'Target' to the package."""
        assert target.name not in self.targets, \
                "duplicate target %s in package %s" % (target.name, self.relpath)
        self.targets[target.name] = target

    def outpath(self, env, *parts):
        """Creates a path to an output resource within this package."""
        return self.project.outpath(env, self.relpath, *parts)

    def inpath(self, *parts):
        """Creates a path to an input resource within this package."""
        return self.project.inpath(self.relpath, *parts)

    def linkpath(self, *parts):
        """Creates a path into the 'latest' symlinks for this package."""
        return self.project.linkpath(self.relpath, *parts)

    def make_absolute(self, ident):
        """Makes an ident, which may be relative to this package, absolute."""
        if ident.startswith(':'):
            return self.project.alias + '//' + self.relpath + ident
        if ident.startswith('//'):
            return self.project.alias + ident
        if '//' in ident:
            return ident
        raise Exception('Unexpected ident: %r' % ident)

    def find_target(self, ident):
        """Finds a target relative to this package. This enables local
        references using the ':foo' syntax."""
        if ident.startswith(':'):
            return self.project.find_target(
                self.project.alias + '//' + self.relpath + ident)
        return self.project.find_target(ident)
