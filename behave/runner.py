# -*- coding: utf-8 -*-

from __future__ import with_statement

import contextlib
import os.path
import StringIO
import sys
import traceback
import warnings
import weakref

import multiprocessing,time

from behave import parser
from behave import matchers
from behave import step_registry
from behave.formatter import formatters
from behave.configuration import ConfigError
from behave.log_capture import LoggingCapture


class ContextMaskWarning(UserWarning):
    '''Raised if a context variable is being overwritten in some situations.

    If the variable was originally set by user code then this will be raised if
    *behave* overwites the value.

    If the variable was originally set by *behave* then this will be raised if
    user code overwites the value.
    '''
    pass


class Context(object):
    '''Hold contextual information during the running of tests.

    This object is a place to store information related to the tests you're
    running. You may add arbitrary attributes to it of whatever value you need.

    During the running of your tests the object will have additional layers of
    namespace added and removed automatically. There is a "root" namespace and
    additional namespaces for features and scenarios.

    Certain names are used by *behave*; be wary of using them yourself as
    *behave* may overwrite the value you set. These names are:

    .. attribute:: feature

      This is set when we start testing a new feature and holds a
      :class:`~behave.model.Feature`. It will not be present outside of a
      feature (i.e. within the scope of the environment before_all and
      after_all).

    .. attribute:: scenario

      This is set when we start testing a new scenario (including the
      individual scenarios of a scenario outline) and holds a
      :class:`~behave.model.Scenario`. It will not be present outside of the
      scope of a scenario.

    .. attribute:: tags

      The current set of active tags (as a Python set containing instances of
      :class:`~behave.model.Tag` which are basically just glorified strings)
      combined from the feature and scenario. This attribute will not be
      present outside of a feature scope.

    .. attribute:: failed

      This is set in the root namespace as soon as any step fails.

    .. attribute:: table

      This is set at the step level and holds any :class:`~behave.model.Table`
      associated with the step.

    .. attribute:: text

      This is set at the step level and holds any multiline text associated
      with the step.

    .. attribute:: config

      The configuration of *behave* as determined by configuration files and
      command-line options. The attributes of this object are the same as the
      `configuration file settion names`_.

    .. attribute:: active_outline

      This is set for each scenario in a scenario outline and references the
      :class:`~behave.model.Row` that is active for the current scenario. It is
      present mostly for debugging, but may be useful otherwise.

    .. attribute:: log_capture

      If logging capture is enabled then this attribute contains the captured
      logging as an instance of :class:`~behave.log_capture.LoggingCapture`.
      It is not present if logging is not being captured.

    .. attribute:: stdout_capture

      If logging capture is enabled then this attribute contains the captured
      stdout as a StringIO instance. It is not present if stdout is not being
      captured.

    If an attempt made by user code to overwrite one of these variables, or
    indeed by *behave* to overwite a user-set variable, then a
    :class:`behave.runner.ContextMaskWarning` warning will be raised.

    You may use the "in" operator to test whether a certain value has been set
    on the context, for example:

        'feature' in context

    checks whether there is a "feature" value in the context.

    Values may be deleted from the context using "del" but only at the level
    they are set. You can't delete a value set by a feature at a scenario level
    but you can delete a value set for a scenario in that scenario.

    .. _`configuration file settion names`: behave.html#configuration-files
    '''
    BEHAVE = 'behave'
    USER = 'user'

    def __init__(self, runner):
        self._runner = weakref.proxy(runner)
        self._config = runner.config
        d = self._root = {
            'failed': False,
            'config': self._config,
            'active_outline': None,
        }
        self._stack = [d]
        self._record = {}
        self._origin = {}
        self._mode = self.BEHAVE

    def _push(self):
        self._stack.insert(0, {})

    def _pop(self):
        self._stack.pop(0)

    @contextlib.contextmanager
    def user_mode(self):
        self._mode = self.USER
        yield
        self._mode = self.BEHAVE

    def _set_root_attribute(self, attr, value):
        for frame in self.__dict__['_stack']:
            if frame is self.__dict__['_root']:
                continue
            if attr in frame:
                record = self.__dict__['_record'][attr]
                params = {
                    'attr': attr,
                    'filename': record[0],
                    'line': record[1],
                    'function': record[3],
                }
                self._emit_warning(attr, params)

        self.__dict__['_root'][attr] = value
        if attr not in self._origin:
            self._origin[attr] = self._mode

    def _emit_warning(self, attr, params):
        msg = ''
        if self._mode is self.BEHAVE and self._origin[attr] is not self.BEHAVE:
            msg = "behave runner is masking context attribute '%(attr)s' " \
                  "orignally set in %(function)s (%(filename)s:%(line)s)"
        elif self._mode is self.USER:
            if self._origin[attr] is not self.USER:
                msg = "user code is masking context attribute '%(attr)s' " \
                      "orignally set by behave"
            elif self._config.verbose:
                msg = "user code is masking context attribute " \
                    "'%(attr)s'; see the tutorial for what this means"
        if msg:
            msg = msg % params
            warnings.warn(msg, ContextMaskWarning, stacklevel=3)

    def _dump(self):
        for level, frame in enumerate(self._stack):
            print 'Level %d' % level
            print repr(frame)

    def __getattr__(self, attr):
        if attr[0] == '_':
            return self.__dict__[attr]
        for frame in self._stack:
            if attr in frame:
                return frame[attr]
        msg = "'{0}' object has no attribute '{1}'"
        msg = msg.format(self.__class__.__name__, attr)
        raise AttributeError(msg)

    def __setattr__(self, attr, value):
        if attr[0] == '_':
            self.__dict__[attr] = value
            return

        for frame in self._stack[1:]:
            if attr in frame:
                record = self._record[attr]
                params = {
                    'attr': attr,
                    'filename': record[0],
                    'line': record[1],
                    'function': record[3],
                }
                self._emit_warning(attr, params)

        stack_frame = traceback.extract_stack(limit=2)[0]
        self._record[attr] = stack_frame
        frame = self._stack[0]
        frame[attr] = value
        if attr not in self._origin:
            self._origin[attr] = self._mode

    def __delattr__(self, attr):
        frame = self._stack[0]
        if attr in frame:
            del frame[attr]
            del self._record[attr]
        else:
            msg = "'{0}' object has no attribute '{1}' at the current level"
            msg = msg.format(self.__class__.__name__, attr)
            raise AttributeError(msg)

    def __contains__(self, attr):
        if attr[0] == '_':
            return attr in self.__dict__
        for frame in self._stack:
            if attr in frame:
                return True
        return False

    def execute_steps(self, steps):
        '''The steps identified in the "steps" text string will be parsed and
        executed in turn just as though they were defined in a feature file.

        If the execute_steps call fails (either through error or failure
        assertion) then the step invoking it will fail.

        ValueError will be raised if this is invoked outside a feature context.

        Returns boolean False if the steps are not parseable, True otherwise.
        '''
        assert type(steps) is unicode, "Steps must be unicode."
        try:
            assert self.feature
        except (AttributeError, AssertionError):
            raise ValueError('execute_steps() called outside of a feature '
                             'context')

        for step in steps.strip().split('\n'):
            step = step.strip()
            step_obj = self.feature.parser.parse_step(step)
            if step_obj is None:
                return False
            passed = step_obj.run(self._runner, quiet=True)
            if not passed:
                # -- ISSUE #96: Provide more substep info to diagnose problem.
                more = step_obj.error_message
                assert False, \
                       "Sub-step failed: %s\nSubstep info: %s" % (step, more)
        return True


def exec_file(filename, globals={}, locals=None):
    if locals is None:
        locals = globals
    locals['__file__'] = filename
    if sys.version_info[0] == 3:
        with open(filename) as f:
            exec(f.read(), globals, locals)
    else:
        execfile(filename, globals, locals)

def path_getrootdir(path):
    """
    Extract rootdir from in a platform independent way.

    POSIX-PATH EXAMPLE:
        rootdir = path_getrootdir("/foo/bar/one.feature")
        assert rootdir == "/"

    WINDOWS-PATH EXAMPLE:
        rootdir = path_getrootdir(r"D:\foo\bar\one.feature")
        assert rootdir == r"D:\"
    """
    drive, _ = os.path.splitdrive(path)
    if drive:
        # -- WINDOWS:
        return drive + os.path.sep
    # -- POSIX:
    return os.path.sep


class PathManager(object):
    paths = None

    def __enter__(self):
        self.paths = []

    def __exit__(self, *crap):
        for path in self.paths:
            sys.path.remove(path)

    def add(self, path):
        assert self.paths is not None, \
            self.__class__.__name__ + '.add called outside of context'
        sys.path.insert(0, path)
        self.paths.append(path)


class Runner(object):
    def __init__(self, config):
        self.config = config

        self.hooks = {}

        self.features = []
        self.passed = []
        self.failed = []
        self.undefined = []
        self.skipped = []

        self.path_manager = PathManager()

        self.feature = None

        self.stdout_capture = None
        self.stderr_capture = None
        self.log_capture = None
        self.old_stdout = None

        self.base_dir   = None
        self.context    = None
        self.formatter  = None

    def setup_paths(self):
        if self.config.paths:
            if self.config.verbose:
                print 'Supplied path:', ', '.join('"%s"' % path 
                       for path in self.config.paths)
            base_dir = self.config.paths[0]
            if base_dir.startswith('@'):
                # -- USE: behave @features.txt
                base_dir = base_dir[1:]
                files = self.feature_files()
                if files:
                    base_dir = os.path.dirname(files[0])
            base_dir = os.path.abspath(base_dir)

            # supplied path might be to a feature file
            if os.path.isfile(base_dir):
                if self.config.verbose:
                    print 'Primary path is to a file so using its directory'
                base_dir = os.path.dirname(base_dir)
        else:
            if self.config.verbose:
                print 'Using default path "./features"'
            base_dir = os.path.abspath('features')

        # Get the root. This is not guaranteed to be '/' because Windows.
        root_dir = path_getrootdir(base_dir)
        new_base_dir = base_dir

        while True:
            if self.config.verbose:
                print 'Trying base directory:', new_base_dir

            if os.path.isdir(os.path.join(new_base_dir, 'steps')):
                break
            if os.path.isfile(os.path.join(new_base_dir, 'environment.py')):
                break
            if new_base_dir == root_dir:
                break

            new_base_dir = os.path.dirname(new_base_dir)

        if new_base_dir == root_dir:
            if self.config.verbose:
                if not self.config.paths:
                    print 'ERROR: Could not find "steps" directory. Please '\
                        'specify where to find your features.'
                else:
                    print 'ERROR: Could not find "steps" directory in your '\
                        'specified path "%s"' % base_dir
            raise ConfigError('No steps directory in "%s"' % base_dir)

        base_dir = new_base_dir

        for dirpath, dirnames, filenames in os.walk(base_dir):
            if [fn for fn in filenames if fn.endswith('.feature')]:
                break
        else:
            if self.config.verbose:
                if not self.config.paths:
                    print 'ERROR: Could not find any "<name>.feature" files. '\
                        'Please specify where to find your features.'
                else:
                    print 'ERROR: Could not find any "<name>.feature" files '\
                        'in your specified path "%s"' % base_dir
            raise ConfigError('No feature files in "%s"' % base_dir)

        self.base_dir = base_dir
        self.path_manager.add(base_dir)
        if not self.config.paths:
            self.config.paths = [base_dir]

        if base_dir != os.getcwd():
            self.path_manager.add(os.getcwd())

    def load_hooks(self, filename='environment.py'):
        hooks_path = os.path.join(self.base_dir, filename)
        if os.path.exists(hooks_path):
            exec_file(hooks_path, self.hooks)

    def load_step_definitions(self, extra_step_paths=[]):
        steps_dir = os.path.join(self.base_dir, 'steps')

        # allow steps to import other stuff from the steps dir
        sys.path.insert(0, steps_dir)

        step_globals = {
            'step_matcher': matchers.step_matcher,
        }
        # -- Default matcher can be overridden in "environment.py" hook.
        default_matcher = matchers.current_matcher

        for step_type in ('given', 'when', 'then', 'step'):
            decorator = getattr(step_registry, step_type)
            step_globals[step_type] = decorator
            step_globals[step_type.title()] = decorator

        for path in [steps_dir] + list(extra_step_paths):
            for name in os.listdir(path):
                if name.endswith('.py'):
                    # -- LOAD STEP DEFINITION:
                    # Reset to default matcher after each step-definition.
                    # A step-definition may change the matcher 0..N times.
                    exec_file(os.path.join(path, name), step_globals)
                    matchers.current_matcher = default_matcher

        # -- CLEANUP: Clean up the path.
        sys.path.pop(0)

    def run_hook(self, name, context, *args):
        if not self.config.dry_run and (name in self.hooks):
            with context.user_mode():
                self.hooks[name](context, *args)

    @staticmethod
    def parse_features_file(features_filename):
        """
        Read textual file, ala '@features.txt'
        :param features_filename:  Name of features file.
        :return: List of feature names.
        """
        if features_filename.startswith('@'):
            features_filename = features_filename[1:]
        here  = os.path.dirname(features_filename) or "."
        files = []
        for line in open(features_filename).readlines():
            line = line.strip()
            if not line:
                continue    # SKIP: Over empty line(s).
            elif line.startswith('#'):
                continue    # SKIP: Over comment line(s).
            files.append(os.path.normpath(os.path.join(here, line)))
        return files

    def feature_files(self):
        files = []
        for path in self.config.paths:
            if os.path.isdir(path):
                for dirpath, dirnames, filenames in os.walk(path):
                    for filename in filenames:
                        if filename.endswith('.feature'):
                            files.append(os.path.join(dirpath, filename))
            elif path.startswith('@'):
                # -- USE: behave @list_of_features.txt
                files.extend(self.parse_features_file(path[1:]))
            elif os.path.exists(path):
                files.append(path)
            else:
                raise Exception("Can't find path: " + path)
        return files

    def run(self):
        with self.path_manager:
            self.setup_paths()
            return self.run_with_paths()

    def run_with_paths(self):

        if getattr(self.config,'proc_count'):
            return self.run_multiproc() 

        self.load_hooks()
        self.load_step_definitions()

        context = self.context = Context(self)
        # -- ENSURE: context.execute_steps() works in weird cases (hooks, ...)
        self.setup_capture()
        stream = self.config.output
        failed = False
        failed_count = 0

        self.run_hook('before_all', context)

        for filename in self.feature_files():
            if self.config.exclude(filename):
                continue

            feature = parser.parse_file(os.path.abspath(filename),
                                        language=self.config.lang)

            self.features.append(feature)
            self.feature = feature

            self.formatter = formatters.get_formatter(self.config, stream)
            self.formatter.uri(filename)

            failed = feature.run(self)
            if failed:
                failed_count += 1

            self.formatter.close()
            stream.write('\n')
            for reporter in self.config.reporters:
                reporter.feature(feature)

            if failed and self.config.stop:
                break

        self.run_hook('after_all', context)
        for reporter in self.config.reporters:
            reporter.end()

        failed = (failed_count > 0)
        return failed

    def run_multiproc(self):
        """
        TODO: I know this needs refactoring, just gimme awhile. I'll get to it.
        """

        self.parallel_element = getattr(self.config,'parallel_element')
        if self.parallel_element != 'feature' and self.parallel_element != 'scenario':
            print "ERROR: When using --processes, --parallel-element option must be set to 'feature' or 'scenario'"
            return -1
        
        self.load_hooks()
        self.load_step_definitions()

        context = self.context = Context(self)
        def do_nothing(obj2,obj3):
            pass
        context._emit_warning = do_nothing

        self.setup_capture()
        stream = self.config.output

        self.run_hook('before_all', context)

        for filename in self.feature_files():
            if self.config.exclude(filename):
                continue

            feature = parser.parse_file(os.path.abspath(filename),
                        language=self.config.lang)

            self.features.append(feature)

        self.formatter = formatters.get_formatter(self.config, stream)
        self.formatter.uri(filename)

        self.joblist_index_queue = multiprocessing.Manager().JoinableQueue()
        self.scenarioresults = multiprocessing.Manager().JoinableQueue()
        self.featureresults = multiprocessing.Manager().JoinableQueue()
        self.resultsqueue = multiprocessing.Manager().JoinableQueue()
 
        self.joblist = []    

        scenario_count = 0    
        feature_count = 0    
        for feature in self.features:
            if self.parallel_element == 'feature' or 'serial' in feature.tags:
                self.joblist.append(feature)
                self.joblist_index_queue.put(feature_count+scenario_count)
                feature_count += 1
                continue
            for scenario in feature.scenarios:
                if scenario.type == 'scenario':
                    self.joblist.append(scenario)
                    self.joblist_index_queue.put(feature_count+scenario_count)
                    scenario_count += 1
                else:
        	    for subscenario in scenario.scenarios:
                        self.joblist.append(subscenario)
                        self.joblist_index_queue.put(feature_count+scenario_count)
                        scenario_count += 1

        proc_count = int(getattr(self.config,'proc_count'))
        print "INFO:",scenario_count,"scenario(s) and",feature_count,"feature(s) queued for consideration by",\
        proc_count,"workers. Some may be skipped if the -t option was given..."
        time.sleep(2)

        procs = []
        for i in range(proc_count):
            p = multiprocessing.Process(target=self.worker,args=(i,))
            procs.append(p)
            p.start()

        [p.join() for p in procs]

        self.run_hook('after_all', context)

        features_passed = 0
        features_failed = 0
        features_skipped = 0
        
        scenarios_passed = 0
        scenarios_failed = 0
        scenarios_skipped = 0

        steps_passed = 0
        steps_failed = 0
        steps_skipped = 0
        steps_undefined = 0

        combined_features_from_scenarios_results = {}

        while not self.resultsqueue.empty():
            print "\n"*3
            print "_"*75
            jobresult = self.resultsqueue.get()
            print jobresult['reportinginfo']

            if jobresult['jobtype'] != 'feature':
                uniquekey = jobresult['uniquekey']
                if uniquekey in combined_features_from_scenarios_results:
                    combined_features_from_scenarios_results[uniquekey] += '|'+jobresult['status']
                else:
                    combined_features_from_scenarios_results[uniquekey] = jobresult['status']

                if jobresult['status'] == 'passed':
                    scenarios_passed += 1 
                elif jobresult['status'] == 'failed':
                    scenarios_failed += 1 
                elif jobresult['status'] == 'skipped':
                    scenarios_skipped += 1
            else:
                if jobresult['status'] == 'passed':
                    features_passed += 1 
                elif jobresult['status'] == 'failed':
                    features_failed += 1 
                elif jobresult['status'] == 'skipped':
                    features_skipped += 1
            steps_passed += jobresult['steps_passed'] 
            steps_failed += jobresult['steps_failed'] 
            steps_skipped += jobresult['steps_skipped'] 
            steps_undefined += jobresult['steps_undefined']

            if jobresult['jobtype'] == 'feature': 
                scenarios_passed += jobresult['scenarios_passed'] 
                scenarios_failed += jobresult['scenarios_failed'] 
                scenarios_skipped += jobresult['scenarios_skipped'] 
                
        for uniquekey in combined_features_from_scenarios_results:
            if 'failed' in combined_features_from_scenarios_results[uniquekey]:
                features_failed += 1
            elif 'passed' in combined_features_from_scenarios_results[uniquekey]:
                features_passed += 1
            else:
                features_skipped += 1

        print "\n"*3
        print "_"*75
        print features_passed,"features passed,",features_failed,"failed,",features_skipped,"skipped"            
        print scenarios_passed,"scenarios passed,",scenarios_failed,"failed,",scenarios_skipped,"skipped"            
        print steps_passed,"steps passed,",steps_failed,"failed,",steps_skipped,"skipped,",steps_undefined,"undefined"
        print "\n"

        return features_failed 
    
    def worker(self,proc_number):
    	while 1:
    	    try:
    		    joblist_index = self.joblist_index_queue.get_nowait()
    	    except Exception,e:
    		    break
            current_job = self.joblist[joblist_index]
            writebuf = StringIO.StringIO()

            self.setfeature(current_job)
            self.setformatterbuffer(current_job,writebuf)

            start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            current_job.run(self)
            end_time = time.strftime("%Y-%m-%d %H:%M:%S")

            sys.stderr.write("* ")

            job_report_text = self.generatereport(proc_number,current_job,start_time,end_time,writebuf)

            if job_report_text:
                results = {}
                results['steps_passed'] = 0
                results['steps_failed'] = 0
                results['steps_skipped'] = 0
                results['steps_undefined'] = 0
                results['jobtype'] = current_job.type
                results['reportinginfo'] = job_report_text
                results['status'] = current_job.status
                if current_job.type != 'feature':
                    results['uniquekey'] = current_job.filename+current_job.feature.name
                else:
                    results['scenarios_passed'] = 0
                    results['scenarios_failed'] = 0
                    results['scenarios_skipped'] = 0
                    self.countscenariostatus(current_job,results)
                self.countstepstatus(current_job,results)
                self.resultsqueue.put(results)
    
    def setfeature(self,current_job):
        if current_job.type == 'feature':
            self.feature = current_job
        else:
            self.feature = current_job.feature

    def setformatterbuffer(self,current_job,writebuf):
        self.formatter = formatters.get_formatter(self.config, writebuf)
        if current_job.type == 'feature':
            self.formatter.uri(current_job.filename)
        else:
            self.formatter.uri(current_job.feature.filename)

    def generatereport(self,proc_number,current_job,start_time,end_time,writebuf):
        if not writebuf.pos:
            return ""
        reportheader = start_time+"|WORKER"+str(proc_number)+" START|"

        if current_job.type == 'feature':
            reportheader += "Feature:"+current_job.name+"|"+current_job.filename
        else:
            reportheader += "Scenario:"+current_job.name+"|Feature:"+current_job.feature.name+"|"+current_job.filename

        reportfooter = end_time+"|WORKER"+str(proc_number)+" END|"

        if current_job.type == 'feature':
            reportfooter += "Feature:"+current_job.name+"|status:"+current_job.status+"|"+\
            current_job.filename+"|Duration:"+str(current_job.duration)
        else:
            reportfooter += "Scenario:"+current_job.name+"|Feature:"+current_job.feature.name+"|"+\
            "status:"+current_job.status+"|"+current_job.filename+\
            "|Duration:"+str(current_job.duration)

        if self.config.format[0] == 'plain' and len(current_job.tags):
            tags = "@"
            for tag in current_job.tags:
                tags += tag+" "
            reportheader += "\n"+tags
       
        if current_job.status == 'failed':
            self.getskippedsteps(current_job,writebuf)

        writebuf.seek(0)
        return reportheader+writebuf.read()+"\n"+reportfooter 

    def getskippedsteps(self,current_job,writebuf):
        if current_job.type != 'scenario':
            [self.getskippedsteps(s,writebuf) for s in current_job.scenarios]
        else: 
            for step in current_job.steps:
                if step.status == 'skipped':
                    writebuf.write("Skipped because of error - Scenario:"+current_job.name+"|"+\
                    "step:"+step.name+"\n")

    def countscenariostatus(self,current_job,results):
        if current_job.type != 'scenario':
            [self.countscenariostatus(s,results) for s in current_job.scenarios]
        else:
            if current_job.status == 'passed':
                results['scenarios_passed'] += 1 
            if current_job.status == 'failed':
                results['scenarios_failed'] += 1 
            if current_job.status == 'skipped':
                results['scenarios_skipped'] += 1 

    def countstepstatus(self,current_job,results):
        if current_job.type != 'scenario':
            [self.countstepstatus(s,results) for s in current_job.scenarios]
        else:
            for step in current_job.steps:
                if step.status == 'passed':
                    results['steps_passed'] += 1
                elif step.status == 'failed':
                    results['steps_failed'] += 1
                elif step.status == 'skipped':
                    results['steps_skipped'] += 1
                else:
                    results['steps_undefined'] += 1

    def setup_capture(self):
        if self.config.stdout_capture:
            self.stdout_capture = StringIO.StringIO()
            self.context.stdout_capture = self.stdout_capture

        if self.config.stderr_capture:
            self.stderr_capture = StringIO.StringIO()
            self.context.stderr_capture = self.stderr_capture

        if self.config.log_capture:
            self.log_capture = LoggingCapture(self.config)
            self.log_capture.inveigle()
            self.context.log_capture = self.log_capture

    def start_capture(self):
        if self.config.stdout_capture:
            self.old_stdout = sys.stdout
            sys.stdout = self.stdout_capture

        if self.config.stderr_capture:
            self.old_stderr = sys.stderr
            sys.stderr = self.stderr_capture

    def stop_capture(self):
        if self.config.stdout_capture:
            sys.stdout = self.old_stdout

        if self.config.stderr_capture:
            sys.stderr = self.old_stderr

    def teardown_capture(self):
        if self.config.log_capture:
            self.log_capture.abandon()
