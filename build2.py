#!/usr/bin/env python
from __future__ import print_function, division

import argparse
from collections import defaultdict
import datetime
import json
import psutil
import os
import shutil
import subprocess
import time
import networkx as nx
import sys


from conda_build.metadata import parse, MetaData

CONDA_BUILD_CACHE=os.environ.get("CONDA_BUILD_CACHE")

class PopenWrapper(object):
    # Small wrapper around subprocess.Popen to allow memory usage monitoring

    def __init__(self, *args, **kwargs):
        self.elapsed = None
        self.rss = None
        self.vms = None
        self.returncode=None
        self.disk = None

        #Process executed immediately
        self._execute(*args, **kwargs)

    def _execute(self, *args, **kwargs):
        # The polling interval (in seconds)
        time_int = kwargs.pop('time_int', 1)

        # Create a process of this (the parent) process
        parent = psutil.Process(os.getpid())
        initial_usage = psutil.disk_usage(sys.prefix).used

        # Using the convenience Popen class provided by psutil
        start_time = time.time()
        _popen = psutil.Popen(*args, **kwargs)
        try:
            while _popen.is_running():
                #We need to get all of the children of our process since our process spawns other processes
                # Collect all of the child processes

                try:
                    # We use the parent process to get mem usage of all spawned processes
                    child_pids = [_.memory_info() for _ in parent.children(recursive=True) if _.is_running()]
                    # Sum the memory usage of all the children together (2D columnwise sum)
                    rss, vms = [sum(_) for _ in zip(*child_pids)]

                    self.rss = max(rss, self.rss)
                    self.vms = max(vms, self.vms)

                    # Get disk usage
                    used_disk = initial_usage - psutil.disk_usage(sys.prefix).used
                    self.disk = max(used_disk, self.disk)

                except psutil.AccessDenied as e:
                    if _popen.status() == psutil.STATUS_ZOMBIE:
                        _popen.wait()

                time.sleep(time_int)
                self.elapsed = time.time() - start_time
                self.returncode = _popen.returncode
        except KeyboardInterrupt:
            _popen.kill()
            raise

    def __repr__(self):
        return str({'elapsed': self.elapsed,
                    'rss': self.rss,
                    'vms': self.vms,
                    'returncode': self.returncode})

def bytes2human(n):
    # http://code.activestate.com/recipes/578019
    # >>> bytes2human(10000)
    # '9.8K'
    # >>> bytes2human(100001221)
    # '95.4M'
    symbols = ('K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = {}
    for i, s in enumerate(symbols):
        prefix[s] = 1 << (i + 1) * 10
    for s in reversed(symbols):
        if n >= prefix[s]:
            value = float(n) / prefix[s]
            return '%.1f%s' % (value, s)
    return "%sB" % n


def read_recipe(path):
    return MetaData(path)

def describe_meta(meta):
    """Return a dictionary that describes build info of meta.yaml"""

    # Things we care about and need fast access to:
    #   1. Package name and version
    #   2. Build requirements
    #   3. Build number
    #   4. Recipe directory
    d = {}

    d['build'] = meta.get_value('build/number', 0)
    d['depends'] = format_deps(meta.get_value('requirements/build'))
    d['version'] = meta.get_value('package/version')
    return d


def format_deps(deps):
    d = {}
    for x in deps:
        x = x.strip().split()
        if len(x) == 2:
            d[x[0]] = x[1]
        else:
            d[x[0]] = ''
    return d

def get_build_deps(recipe):
    return format_deps(recipe.get_value('requirements/build'))

def git_changed_files(git_rev, git_root=''):
    """
    Get the list of files changed in a git revision and return a list of package directories that have been modified.
    """
    files = subprocess.check_output(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', git_rev])
    
    changed = {os.path.dirname(f) for f in files}
    return changed
    
def construct_graph(directory):
    '''
    Construct a directed graph of dependencies from a directory of recipes

    Annotate dependencies that don't have recipes in that directory
    '''

    g = nx.DiGraph()
    build_numbers = {}
    directory = os.path.abspath(directory)
    assert os.path.isdir(directory)

    # get all immediate subdirectories
    recipe_dirs = next(os.walk(directory))[1]
    recipe_dirs = set(x for x in recipe_dirs if not x.startswith('.'))
    changed_recipes = git_changed_files('HEAD')

    for rd in recipe_dirs:
        recipe_dir = os.path.join(directory, rd)
        try:
            pkg = read_recipe(recipe_dir)
            name = pkg.name()
        except:
            continue

        # add package (in case it has no build deps)
        _dirty = True if rd in changed_recipes else False
        g.add_node(name, meta=describe_meta(pkg), recipe=recipe_dir, dirty=_dirty)
        for k, d in get_build_deps(pkg).iteritems():
            g.add_edge(name, k)

    return g

def dirty(graph):
    """
    Return a set of all dirty nodes in the graph
    """
    return {n for n in graph.node if getattr(n, 'dirty', False) == True}

def successors_iter(g, s, nodes):
    for s in g.successors(s):
        nodes.add(s)
        for s in tuple(successors_iter(g, s, nodes)):
            nodes.add(s)
    return nodes

def coalesce(hi_level_builds, targetnum):
    coalesced = defaultdict(lambda: [])
    counts = [(k, len(v)) for k, v in hi_level_builds.items()]
    group = []
    for key, count in sorted(counts, key=lambda x:x[1]):
        group.append(hi_level_builds[key] + [key])
        if sum(map(len, group)) >= targetnum:
            for g in group:
                if g == key:
                    continue
                coalesced[key] += [gi for gi in g if gi not in coalesced[key] and gi != key]
            group = []
    if group:
        for g in group:
            coalesced[key] += [gi for gi in g if gi not in coalesced[key] and gi != key]
    return coalesced

def split_graph(g, targetnum, split_file):
    g = g.copy()
    toposort = nx.topological_sort(g)
    packages_covered = defaultdict(lambda:0)
    degrees = dict(g.degree_iter())

    hi_level_builds = {}
    for hi_level in nx.topological_sort(g):
        if hi_level in packages_covered:
            continue
        succ = tuple(successors_iter(g, hi_level, set()))
        for s in succ:
            packages_covered[s] += 1
        packages_covered[hi_level] += 1
        topo_order = [(s, toposort.index(s)) for s in succ]
        succ_order = sorted(topo_order, key=lambda x: -x[1])
        hi_level_builds[hi_level] = [_[0] for _ in succ_order]
    hi_level_builds = coalesce(hi_level_builds, targetnum)
    with open(split_file, 'w') as f:
        f.write(json.dumps(hi_level_builds))
    return hi_level_builds

def build_order(graph, packages, level=0):
    '''
    Assumes that packages are in graph.
    Builds a temporary graph of relevant nodes and returns it topological sort.
    
    Relevant nodes selected in a breadth first traversal sourced at each pkg in packages.
    '''

    if packages is None:
        tmp_global = graph.subgraph(graph.nodes())
    else:
        packages = set(packages)
        tmp_global = graph.subgraph(packages)

        if level > 0:
            # for each level, add all deps
            _level = level

            currlevel = packages
            while _level > 0:
                newcurr = set()
                for p in currlevel:
                    newcurr.update(set(graph.successors(p)))
                    tmp_global.add_edges_from(graph.edges_iter(p))
                currlevel = newcurr
                _level -= 1

    #copy relevant node data to tmp_global
    for n in tmp_global.nodes_iter():
        tmp_global.node[n] = graph.node[n]

    return tmp_global, nx.topological_sort(tmp_global, reverse=True)


def make_deps(graph, package, dry=False, extra_args='', level=0, autofail=True):
    g, order = build_order(graph, package, level=level)

    # Filter out any packages that don't have recipes
    order = [pkg for pkg in order if g.node[pkg].get('meta')]
    print("Build order:\n{}".format('\n'.join(order)))

    failed = set()
    build_times = {x:None for x in order}
    for pkg in order:
        print("Building ", pkg)
        try:
            # Autofail package if any dependency build failed
            if any(p in failed for p in order):
                print(failed)
                failed_deps = [p for p in g.node[pkg]['meta']['depends'].keys() if p in failed]
                print("Building {} failed because one or more of its dependencies failed to build: ".format(pkg), end=' ')
                print(', '.join(failed_deps))
                failed.add(pkg)
                continue
            build_time = make_pkg(g.node[pkg], dry=dry, extra_args=extra_args)
            build_times[pkg] = build_time
        except KeyboardInterrupt:
            return failed
        except subprocess.CalledProcessError:
            failed.add(pkg)
            continue

    return list(set(order)-failed), list(failed), build_times


def make_pkg(package, dry=False, extra_args=''):
    meta, path = package['meta'], package['recipe']
    print("===========> Building ", path)
    if not dry:
        try:
            extra_args = extra_args.split()
            args = ['conda', 'build', '-q'] + extra_args + [path]
            print("+ " + ' '.join(args))
            p = PopenWrapper(args, time_int=1)
            return p
        except subprocess.CalledProcessError as e:
            print("Build failed with errorcode: ", e.returncode)
            print(e)
            raise


def submit_one(args):
    '''
    Adjusts binstar_template.yml
    base on
        user
        queue
        build arguments
        platforms
    Comes up with a package name for user
    Creates package if it doesn't exist
    Submits package
    Prints out the command you need to tail the build
    returns 0 if okay
    '''
    import jinja2
    js_file, key = args.json_file_key
    with open(js_file, 'r') as f:
        js = json.load(f)
    with open(os.path.join(os.path.dirname(__file__), 'binstar_template.yml')) as f:
        contents = f.read()
        t = jinja2.Template(contents)
        package = 'protoci-' + key
        info = (os.path.basename(js_file), key)
        platforms = "".join(" - {}\n".format(p) for p in args.platforms)
        binstar_yml = t.render(PACKAGE=package,
                               USER=args.user,
                               PLATFORMS=platforms,
                               BUILD_ARGS='./ build ' +\
                                          '-json-file-key {0} {1}'.format(*info))
        with open(os.path.join(args.path, '.binstar.yml'), 'w') as f:
            f.write(binstar_yml)
    full_package = '{0}/{1}'.format(args.user, package)
    cmd = ['anaconda', 'build', 'list-all', full_package]
    print('Check to see if', full_package, 'exists:', cmd)
    proc = subprocess.Popen(cmd)
    if proc.wait():
        cmd = ['anaconda', 'package','--create', full_package]
        print("prepare to create package", cmd)
        if not args.dry:
            create = subprocess.Popen(cmd, cwd=args.path)
            if create.wait():
                raise ValueError('Could not create {}'.format(full_package))

    user_queue = '{0}/{1}'.format(args.user, args.queue)
    cmd = ['anaconda', 'build',
           'submit', './', '--queue',
           user_queue]
    print('prepare to submit', cmd)
    if args.dry:
        return 0
    proc =  subprocess.Popen(cmd, cwd=args.path, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    ret = proc.wait()
    out = proc.stdout.read().decode()
    tail = [line for line in out.split('\n')
            if 'tail' in line and full_package in line]
    if len(tail):
        tail = tail[0]
    else:
        print("Apparently something wrong with:", out)
        time.sleep(10)
    print('TAIL:\t', tail)
    return ret


def submit_full_json(args):
    ''' Given -full-json, run every package tree
    in a json that was created by split action, typically
    called package_tree.js
    '''
    with open(args.full_json, 'r') as f:
        tree = json.load(f)
        print('{} high level packages'.format(len(tree)))
        print('\twith total packages:',
              len(tree) + sum(map(len, tree.values())))
        for key in tree:
            print('Key: ', key, len(tree[key])+1, 'packages to build/test')
            args.json_file_key = (args.full_json, key)
            submit_one(args)
    return 0

def pre_submit_clean_up(args):
    '''Copies files from patterns like:

    ./special_cases/<package-name>/run_test.sh

    to

    args.path/<package-name>/run_test.sh

    (Helpful if anaconda-build needs mods)
    '''
    special = os.path.join(os.path.dirname(__file__), 'special_cases')
    for dirr in os.listdir(special):
        for fil in os.listdir(os.path.join(special, dirr)):
            full_file = os.path.join(special, dirr, fil)
            if not os.path.exists(os.path.join(args.path, dirr)):
                continue
            target = os.path.join(args.path, dirr, fil)
            print('Copy', full_file, 'to', target)
            print('Copy', full_file, 'to', target+'_removed')
            shutil.copy(full_file, target + '_removed')
            shutil.copy(full_file, target)

    this_file = os.path.basename(__file__)
    build2_in_other_dir = os.path.abspath(os.path.join(args.path, this_file))
    shutil.copy(__file__, build2_in_other_dir)
    print('Copy',__file__,'to', build2_in_other_dir)
    package_tree_file = os.path.abspath(args.full_json or args.json_file_key[0])
    n = datetime.datetime.now()
    datestr = "_".join(map(str, (n.year, n.month, n.day, n.hour, n.minute, n.second)))
    branch_name = 'build_' + datestr
    print('Make a scratch git branch in', os.path.abspath(args.path))
    subprocess.check_output(['git', 'checkout', '-b', branch_name], cwd=args.path)
    subprocess.check_output(['git', 'add', build2_in_other_dir, package_tree_file], cwd=args.path)
    print(subprocess.Popen(
          ['git', 'commit', '-m',
          'commit build2.py and the package json for anaconda-build'],
          cwd=args.path).communicate())


def submit_helper(args):
    pre_submit_clean_up(args)
    if 'submit' in sys.argv:
        if args.full_json:
            return submit_full_json(args)
        else:
            assert len(args.json_file_key) >= 2
            arg1 = args.json_file_key[0]
            hi_level = args.json_file_key[1:]
            for key in hi_level:
                args.json_file_key = (arg1, key)
                ret_val = submit_one(args)
                if ret_val:
                    return ret_val
    return 0



def cli(parse_this=None):
    p = argparse.ArgumentParser()
    p.add_argument("path", default='.')
    subp = p.add_subparsers(help="Build or split to make json of package "
                                 "order/grouping. \n\tChoices: %(choices)s")
    build_parser = subp.add_parser('build')
    build_pkgs = build_parser.add_mutually_exclusive_group()
    build_pkgs.add_argument("-build", action='append', default=[])
    build_pkgs.add_argument("-buildall", action='store_true')
    build_pkgs.add_argument('-json-file-key', default=[], nargs="+",
                            help="Example: -json-file-key package_tree.js libnetcdf pysam")
    build_parser.add_argument("-dry", action='store_true', default=False,
                              help="Dry run")
    build_parser.add_argument("-api", action='store_true', dest='recompile',
                              default=False)
    build_parser.add_argument("-args", action='store', dest='cbargs', default='')
    build_parser.add_argument("-l", type=int, action='store', dest='level', default=0)
    build_parser.add_argument("-noautofail", action='store_false', dest='autofail', default=True)
    split_parser = subp.add_parser('split')
    split_parser.add_argument('-t','--targetnum', type=int,
                              default=10,
                              help="How many packages in one anaconda "
                                   "build submission typically.")
    split_parser.add_argument('-s','--split-files',type=str,default="package_tree.js")
    submit_parser = subp.add_parser('submit')
    json_read_choice = submit_parser.add_mutually_exclusive_group()
    json_read_choice.add_argument('-json-file-key',
                                  default=[], nargs=2,
                                help="Example: -json-file-key package_tree.js libnetcdf")
    json_read_choice.add_argument('-full-json',
                                  type=str,
                                  help="Build all packages named in json of splits")
    submit_parser.add_argument('-user', default='conda-team',
                               help="Anaconda username. Default: %(default)s")
    submit_parser.add_argument('-queue', default='build_recipes',
                               help="Anaconda build queue. Default: %(default)s")
    submit_parser.add_argument('-dry', action='store_true', help='Dry run')
    submit_parser.add_argument('-platforms', required=True,
                               help="Some of all of %(default)s",
                               default=['osx-64', 'linux-64','win-64'],
                               nargs="+")
    if parse_this is None:
        args = p.parse_args()
    else:
        args = p.parse_args(parse_this)
    print('Running build2.py with args of', args)
    if getattr(args, 'json_file_key', None):
        assert len(args.json_file_key) == 2, 'Should be 2 args: json_filename key'
    return args

if __name__ == "__main__":

    args = cli()
    print("%s" % (getattr(args,'build','')))
    print("-------------------------------")
    if 'submit' in sys.argv:
        sys.exit(submit_helper(args))
    g = construct_graph(args.path)
    if getattr(args, 'split_files', None) is not None:
        split_graph(g, args.targetnum, args.split_files)
        print("See ", args.split_files, 'for split packages')
        sys.exit(0)
    try:
        if args.buildall:
            args.build = None
        if args.json_file_key:
            with open(args.json_file_key[0]) as f:
                packages = json.load(f)[args.json_file_key[1]]
                packages += args.json_file_key[1:]
                for package in packages:
                    package = g.node[package]
                    if not 'meta' in package:
                        continue
                    make_pkg(package, dry=args.dry, extra_args=args.cbargs)
                sys.exit(0)
        success, fail, times = make_deps(g, args.build, args.dry,
                                         extra_args=args.cbargs,
                                         level=args.level,
                                         autofail=args.autofail)

        print("BUILD SUMMARY:")
        print("SUCCESS: [{}]".format(', '.join(success)))
        print("FAIL: [{}]".format(', '.join(fail)))

        # Sum memory usage and print elapsed times.
        r, v, e = 0, 0, 0
        print("Build stats: Package, Elapsed time, Mem Usage, Disk Usage")
        for k, i in times.items():
            r, v = max(i.rss, r), max(i.vms, r)
            e += i.elapsed
            print("{}\t\t{:.2f}s\t{}\t{}".format(k, e, bytes2human(i.rss), bytes2human(i.disk)))
        r, v = bytes2human(r), bytes2human(v)
        print("Max Memory Usage (RSS/VMS): {}/{}".format(r, v))
        print("Total elapsed time: {:.2f}m".format(e/60))

        sys.exit(len(fail))
    except:
        raise
