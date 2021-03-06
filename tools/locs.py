#!/usr/bin/env python3
# Copyright 2018 the V8 project authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

""" locs.py - Count lines of code before and after preprocessor expansion
  Consult --help for more information.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARGPARSE = argparse.ArgumentParser(
    description=("A script that computes LoC for a build dir or from a"
                 "compile_commands.json file"),
    epilog="""Examples:
 Count with default settings for build in out/Default:
   locs.py --build-dir out/Default
 Count with default settings according to given compile_commands file:
   locs.py --compile-commands compile_commands.json
 Count only a custom group of files settings for build in out/Default:
   tools/locs.py --build-dir out/Default
                 --group src-compiler '\.\./\.\./src/compiler'
                 --only src-compiler
 Report the 10 files with the worst expansion:
   tools/locs.py --build-dir out/Default --worst 10
 Report the 10 files with the worst expansion in src/compiler:
   tools/locs.py --build-dir out/Default --worst 10
                 --group src-compiler '\.\./\.\./src/compiler'
                 --only src-compiler
 Report the 10 largest files after preprocessing:
   tools/locs.py --build-dir out/Default --largest 10
 Report the 10 smallest input files:
   tools/locs.py --build-dir out/Default --smallest 10""",
    formatter_class=argparse.RawTextHelpFormatter
)

ARGPARSE.add_argument(
    '--json',
    action='store_true',
    default=False,
    help="output json instead of short summary")
ARGPARSE.add_argument(
    '--build-dir',
    type=str,
    default="",
    help="Use specified build dir and generate necessary files")
ARGPARSE.add_argument(
    '--echocmd',
    action='store_true',
    default=False,
    help="output command used to compute LoC")
ARGPARSE.add_argument(
    '--compile-commands',
    type=str,
    default='compile_commands.json',
    help="Use specified compile_commands.json file")
ARGPARSE.add_argument(
    '--only',
    action='append',
    default=[],
    help="Restrict counting to report group (can be passed multiple times)")
ARGPARSE.add_argument(
    '--not',
    action='append',
    default=[],
    help="Exclude specific group (can be passed multiple times)")
ARGPARSE.add_argument(
    '--list-groups',
    action='store_true',
    default=False,
    help="List groups and associated regular expressions")
ARGPARSE.add_argument(
    '--group',
    nargs=2,
    action='append',
    default=[],
    help="Add a report group (can be passed multiple times)")
ARGPARSE.add_argument(
    '--largest',
    type=int,
    nargs='?',
    default=0,
    const=3,
    help="Output the n largest files after preprocessing")
ARGPARSE.add_argument(
    '--worst',
    type=int,
    nargs='?',
    default=0,
    const=3,
    help="Output the n files with worst expansion by preprocessing")
ARGPARSE.add_argument(
    '--smallest',
    type=int,
    nargs='?',
    default=0,
    const=3,
    help="Output the n smallest input files")
ARGPARSE.add_argument(
    '--files',
    type=int,
    nargs='?',
    default=0,
    const=3,
    help="Output results for each file separately")

ARGS = vars(ARGPARSE.parse_args())


def MaxWidth(strings):
  max_width = 0
  for s in strings:
    max_width = max(max_width, len(s))
  return max_width


def GenerateCompileCommandsAndBuild(build_dir, compile_commands_file, out):
  if not os.path.isdir(build_dir):
    print("Error: Specified build dir {} is not a directory.".format(
        build_dir), file=sys.stderr)
    exit(1)
  compile_commands_file = "{}/compile_commands.json".format(build_dir)

  print("Generating compile commands in {}.".format(
      compile_commands_file), file=out)

  ninja = "ninja -C {} -t compdb cxx cc > {}".format(
      build_dir, compile_commands_file)
  subprocess.call(ninja, shell=True, stdout=out)
  autoninja = "autoninja -C {}".format(build_dir)
  subprocess.call(autoninja, shell=True, stdout=out)
  return compile_commands_file


class CompilationData:
  def __init__(self, loc, expanded):
    self.loc = loc
    self.expanded = expanded

  def ratio(self):
    return self.expanded / (self.loc+1)

  def to_string(self):
    return "{:>9,} to {:>12,} ({:>5.0f}x)".format(
        self.loc, self.expanded, self.ratio())


class File(CompilationData):
  def __init__(self, file, loc, expanded):
    super().__init__(loc, expanded)
    self.file = file

  def to_string(self):
    return "{} {}".format(super().to_string(), self.file)


class Group(CompilationData):
  def __init__(self, name, regexp_string):
    super().__init__(0, 0)
    self.name = name
    self.count = 0
    self.regexp = re.compile(regexp_string)

  def account(self, unit):
    if (self.regexp.match(unit.file)):
      self.loc += unit.loc
      self.expanded += unit.expanded
      self.count += 1

  def to_string(self, name_width):
    return "{:<{}} ({:>5} files): {}".format(
        self.name, name_width, self.count, super().to_string())


def SetupReportGroups():
  default_report_groups = {"total": '.*',
                           "src": '\\.\\./\\.\\./src',
                           "test": '\\.\\./\\.\\./test',
                           "third_party": '\\.\\./\\.\\./third_party',
                           "gen": 'gen'}

  report_groups = {**default_report_groups, **dict(ARGS['group'])}

  if ARGS['only']:
    for only_arg in ARGS['only']:
      if not only_arg in report_groups.keys():
        print("Error: specified report group '{}' is not defined.".format(
            ARGS['only']))
        exit(1)
      else:
        report_groups = {
            k: v for (k, v) in report_groups.items() if k in ARGS['only']}

  if ARGS['not']:
    report_groups = {
        k: v for (k, v) in report_groups.items() if k not in ARGS['not']}

  if ARGS['list_groups']:
    print_cat_max_width = MaxWidth(list(report_groups.keys()) + ["Category"])
    print("  {:<{}}  {}".format("Category",
                                print_cat_max_width, "Regular expression"))
    for cat, regexp_string in report_groups.items():
      print("  {:<{}}: {}".format(
          cat, print_cat_max_width, regexp_string))

  report_groups = {k: Group(k, v) for (k, v) in report_groups.items()}

  return report_groups


class Results:
  def __init__(self):
    self.groups = SetupReportGroups()
    self.units = []

  def track(self, filename):
    is_tracked = False
    for group in self.groups.values():
      if group.regexp.match(filename):
        is_tracked = True
    return is_tracked

  def recordFile(self, filename, loc, expanded):
    unit = File(filename, loc, expanded)
    self.units.append(unit)
    for group in self.groups.values():
      group.account(unit)

  def maxGroupWidth(self):
    return MaxWidth([v.name for v in self.groups.values()])

  def printGroupResults(self, file):
    for key in sorted(self.groups.keys()):
      print(self.groups[key].to_string(self.maxGroupWidth()), file=file)

  def printSorted(self, key, count, reverse, out):
    for unit in sorted(self.units, key=key, reverse=reverse)[:count]:
      print(unit.to_string(), file=out)


class LocsEncoder(json.JSONEncoder):
  def default(self, o):
    if isinstance(o, File):
      return {"file": o.file, "loc": o.loc, "expanded": o.expanded}
    if isinstance(o, Group):
      return {"name": o.name, "loc": o.loc, "expanded": o.expanded}
    if isinstance(o, Results):
      return {"groups": o.groups, "units": o.units}
    return json.JSONEncoder.default(self, o)


class StatusLine:
  def __init__(self):
    self.max_width = 0

  def print(self, statusline, end="\r", file=sys.stdout):
    self.max_width = max(self.max_width, len(statusline))
    print("{0:<{1}}".format(statusline, self.max_width), end=end, file=file)


class CommandSplitter:
  def __init__(self):
    self.cmd_pattern = re.compile(
        "([^\\s]*\\s+)?(?P<clangcmd>[^\\s]*clang.*)"
        " -c (?P<infile>.*) -o (?P<outfile>.*)")

  def process(self, compilation_unit, temp_file_name):
    cmd = self.cmd_pattern.match(compilation_unit['command'])
    outfilename = cmd.group('outfile') + ".cc"
    infilename = cmd.group('infile')
    infile = Path(compilation_unit['directory']).joinpath(infilename)
    outfile = Path(str(temp_file_name)).joinpath(outfilename)
    return [cmd.group('clangcmd'), infilename, infile, outfile]


def Main():
  compile_commands_file = ARGS['compile_commands']
  out = sys.stdout
  if ARGS['json']:
    out = sys.stderr

  if ARGS['build_dir']:
    compile_commands_file = GenerateCompileCommandsAndBuild(
        ARGS['build_dir'], compile_commands_file, out)

  try:
    with open(compile_commands_file) as file:
      data = json.load(file)
  except FileNotFoundError:
    print("Error: Cannot read '{}'. Consult --help to get started.")
    exit(1)

  result = Results()
  status = StatusLine()

  with tempfile.TemporaryDirectory(dir='/tmp/', prefix="locs.") as temp:
    processes = []
    start = time.time()
    cmd_splitter = CommandSplitter()

    for i, key in enumerate(data):
      if not result.track(key['file']):
        continue
      if not ARGS['json']:
        status.print(
            "[{}/{}] Counting LoCs of {}".format(i, len(data), key['file']))
      clangcmd, infilename, infile, outfile = cmd_splitter.process(key, temp)
      outfile.parent.mkdir(parents=True, exist_ok=True)
      if infile.is_file():
        clangcmd = clangcmd + " -E -P " + \
            str(infile) + " -o /dev/stdout | sed '/^\\s*$/d' | wc -l"
        loccmd = ("cat {}  | sed '\\;^\\s*//;d' | sed '\\;^/\\*;d'"
                  " | sed '/^\\*/d' | sed '/^\\s*$/d' | wc -l").format(
            infile)
        runcmd = " {} ; {}".format(clangcmd, loccmd)
        if ARGS['echocmd']:
          print(runcmd)
        p = subprocess.Popen(
            runcmd, shell=True, cwd=key['directory'], stdout=subprocess.PIPE)
        processes.append({'process': p, 'infile': infilename})

    for i, p in enumerate(processes):
      status.print("[{}/{}] Summing up {}".format(
          i, len(processes), p['infile']), file=out)
      output, err = p['process'].communicate()
      expanded, loc = list(map(int, output.split()))
      result.recordFile(p['infile'], loc, expanded)

    end = time.time()
    if ARGS['json']:
      print(json.dumps(result, ensure_ascii=False, cls=LocsEncoder))
    status.print("Processed {:,} files in {:,.2f} sec.".format(
        len(processes), end-start), end="\n", file=out)
    result.printGroupResults(file=out)

    if ARGS['largest']:
      print("Largest {} files after expansion:".format(ARGS['largest']))
      result.printSorted(
          lambda v: v.expanded, ARGS['largest'], reverse=True, out=out)

    if ARGS['worst']:
      print("Worst expansion ({} files):".format(ARGS['worst']))
      result.printSorted(
          lambda v: v.ratio(), ARGS['worst'], reverse=True, out=out)

    if ARGS['smallest']:
      print("Smallest {} input files:".format(ARGS['smallest']))
      result.printSorted(
          lambda v: v.loc, ARGS['smallest'], reverse=False, out=out)

    if ARGS['files']:
      print("List of input files:")
      result.printSorted(
          lambda v: v.file, ARGS['files'], reverse=False, out=out)

  return 0


if __name__ == '__main__':
  sys.exit(Main())
