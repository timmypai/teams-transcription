"""Bounded complete-line recognizers. No input text survives this module.

Signatures deliberately narrow: other Docker/BuildKit wording is UNKNOWN, not a
root cause. Help is a strict CLI shape and each required flag needs its own typed
row. Absence never proves unsupported. No raw version/help/path is returned.
"""
import re

OPTIONS=('load','provenance','build_context','iidfile')
COMMANDS=('buildx_version','buildx_build_help')
CLASSES=frozenset({'buildx_unavailable','build_option_unsupported','named_context_unsupported',
    'daemon_unreachable','daemon_permission_denied','storage_exhausted','resource_exhausted',
    'public_dependency_fetch_failed','build_step_failed','unknown'})
MAX_CLASSIFICATIONS=8
MISSING="docker: 'buildx' is not a docker command."
FIXED={
 MISSING:('buildx_unavailable','unknown'),
 'ERROR: Cannot connect to the Docker daemon. Is the docker daemon running?':('daemon_unreachable','unknown'),
 'ERROR: permission denied while trying to connect to the Docker daemon socket':('daemon_permission_denied','unknown'),
 'ERROR: failed to build: no space left on device':('storage_exhausted','unknown'),
 'ERROR: failed to build: cannot allocate memory':('resource_exhausted','unknown'),
 'ERROR: failed to solve: failed to fetch anonymous token: unexpected status: 503 Service Unavailable':('public_dependency_fetch_failed','unknown'),
 'ERROR: failed to solve: process "/bin/sh -c exit 1" did not complete successfully: exit code: 1':('build_step_failed','unknown'),
}
REQUIRED={
 'load':r'      --load +Shorthand for "--output=type=docker"',
 'provenance':r'      --provenance string +Shorthand for "--attest=type=provenance"',
 'build_context':r'      --build-context stringArray +Additional build contexts \(e.g., name=path\)',
 'iidfile':r'      --iidfile string +Write the image ID to a file',
}


def classify(line):
    if line in FIXED:
        kind,option=FIXED[line];return dict(classification=kind,option=option)
    for option in OPTIONS:
        if line=='unknown flag: --'+option.replace('_','-'):
            return dict(classification='named_context_unsupported' if option=='build_context' else 'build_option_unsupported',option=option)
    return None


class Projection:
    def __init__(self,probe=None):
        self.probe=probe;self.rows=[];self.incomplete=False
        self.version=False;self.missing=False;self.valid=True;self.count=0
        self.section='usage';self.flags=set();self.previous_flag=False

    def line(self,raw,stream):
        # UTF8 is required even though the reviewed grammars are ASCII. Control
        # codes (including tab/CR/ANSI) are never stripped into valid signatures.
        try:line=raw.decode('utf-8',errors='strict')
        except UnicodeDecodeError:self.incomplete=True;self.valid=False;return
        if any(not c.isprintable() for c in line):
            self.incomplete=True;self.valid=False;return
        row=classify(line)
        if row and row not in self.rows:
            if len(self.rows)<MAX_CLASSIFICATIONS:self.rows.append(row)
            else:self.incomplete=True
        if self.probe is None:return
        if line==MISSING:self.missing=True
        self.count+=1
        if self.probe=='buildx_version':
            self.version=(self.count==1 and stream==0 and re.fullmatch(
                r'github\.com/docker/buildx v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)? [0-9a-f]{7,64}',line) is not None)
            if not self.version:self.valid=False
            return
        if stream!=0:self.valid=False;return
        # Fixed help frame; arbitrary flag descriptions are parsed only for shape
        # and discarded. Missing any required typed row remains UNKNOWN.
        if self.section=='usage':
            if not re.fullmatch(r'Usage: +docker buildx build \[OPTIONS\] PATH \| URL \| -',line):self.valid=False
            self.section='description'
        elif not line:pass
        elif self.section=='description':
            if line!='Start a build':self.valid=False
            self.section='aliases_or_options'
        elif self.section=='aliases_or_options':
            if line=='Aliases:':self.section='aliases'
            elif line=='Options:':self.section='options'
            else:self.valid=False
        elif self.section=='aliases':
            if not re.fullmatch(r'  docker (?:buildx build|build|builder build|image build|buildx b)(?:, docker (?:buildx build|build|builder build|image build|buildx b))*',line):self.valid=False
            self.section='options_header'
        elif self.section=='options_header':
            if line!='Options:':self.valid=False
            self.section='options'
        elif self.section=='options':
            if re.fullmatch(r'  (?:    |-[A-Za-z], )--[a-z][a-z0-9-]*(?: [A-Za-z][A-Za-z0-9]*)? {2,}[^\x00-\x1f\x7f]+',line):
                self.previous_flag=True
                for option,pattern in REQUIRED.items():
                    if re.fullmatch(pattern,line):
                        if option in self.flags:self.valid=False
                        self.flags.add(option)
            elif self.previous_flag and re.fullmatch(r' {10,}[^\x00-\x1f\x7f]+',line):pass
            else:self.valid=False

    def result(self,exit_status,stats):
        state='unknown';options={key:'unknown' for key in OPTIONS}
        complete=not self.incomplete and not stats['truncated']
        if complete and self.missing and exit_status not in (None,0):state='unavailable'
        elif complete and exit_status==0 and self.valid:
            if self.probe=='buildx_version' and self.version:state='available'
            elif self.probe=='buildx_build_help' and self.section=='options' and self.flags==set(OPTIONS):
                state='available';options={key:'supported' for key in OPTIONS}
        return dict(command=self.probe,state=state,exit_status=exit_status,output=stats),options


def empty_diagnostics() -> dict:
    return dict(capabilities=[],required_options={key:'unknown' for key in OPTIONS},
        classifications=[],projection_incomplete=False)
