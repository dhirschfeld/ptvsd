import sys

from _pydev_imps._pydev_saved_modules import threading
from _pydevd_bundle import pydevd_utils
from _pydevd_bundle.pydevd_additional_thread_info import set_additional_thread_info
from _pydevd_bundle.pydevd_comm import (InternalGetThreadStack, internal_get_completions,
    pydevd_find_thread_by_id, InternalSetNextStatementThread, internal_reload_code,
    InternalGetVariable, InternalGetArray, InternalLoadFullValue,
    internal_get_description, internal_get_frame, internal_evaluate_expression, InternalConsoleExec,
    internal_get_variable_json, internal_change_variable, internal_change_variable_json,
    internal_evaluate_expression_json, internal_set_expression_json, internal_get_exception_details_json,
    internal_step_in_thread, internal_run_thread)
from _pydevd_bundle.pydevd_comm_constants import (CMD_THREAD_SUSPEND, file_system_encoding,
    CMD_STEP_INTO_MY_CODE, CMD_STOP_ON_START)
from _pydevd_bundle.pydevd_constants import (get_current_thread_id, set_protocol, get_protocol,
    HTTP_JSON_PROTOCOL, JSON_PROTOCOL, IS_PY3K, DebugInfoHolder, dict_keys)
from _pydevd_bundle.pydevd_net_command_factory_json import NetCommandFactoryJson
from _pydevd_bundle.pydevd_net_command_factory_xml import NetCommandFactory
import pydevd_file_utils
from _pydev_bundle import pydev_log
from _pydevd_bundle.pydevd_breakpoints import LineBreakpoint
from pydevd_tracing import get_exception_traceback_str


class PyDevdAPI(object):

    def run(self, py_db):
        py_db.ready_to_run = True

    def notify_configuration_done(self, py_db):
        py_db.on_configuration_done()

    def notify_disconnect(self, py_db):
        py_db.on_disconnect()

    def set_protocol(self, py_db, seq, protocol):
        set_protocol(protocol.strip())
        if get_protocol() in (HTTP_JSON_PROTOCOL, JSON_PROTOCOL):
            cmd_factory_class = NetCommandFactoryJson
        else:
            cmd_factory_class = NetCommandFactory

        if not isinstance(py_db.cmd_factory, cmd_factory_class):
            py_db.cmd_factory = cmd_factory_class()

        return py_db.cmd_factory.make_protocol_set_message(seq)

    def set_ide_os_and_breakpoints_by(self, py_db, seq, ide_os, breakpoints_by):
        '''
        :param ide_os: 'WINDOWS' or 'UNIX'
        :param breakpoints_by: 'ID' or 'LINE'
        '''
        if breakpoints_by == 'ID':
            py_db._set_breakpoints_with_id = True
        else:
            py_db._set_breakpoints_with_id = False

        self.set_ide_os(ide_os)

        return py_db.cmd_factory.make_version_message(seq)

    def set_ide_os(self, ide_os):
        '''
        :param ide_os: 'WINDOWS' or 'UNIX'
        '''
        pydevd_file_utils.set_ide_os(ide_os)

    def send_error_message(self, py_db, msg):
        sys.stderr.write('pydevd: %s\n' % (msg,))

    def set_show_return_values(self, py_db, show_return_values):
        if show_return_values:
            py_db.show_return_values = True
        else:
            if py_db.show_return_values:
                # We should remove saved return values
                py_db.remove_return_values_flag = True
            py_db.show_return_values = False
        pydev_log.debug("Show return values: %s", py_db.show_return_values)

    def list_threads(self, py_db, seq):
        # Response is the command with the list of threads to be added to the writer thread.
        return py_db.cmd_factory.make_list_threads_message(py_db, seq)

    def request_suspend_thread(self, py_db, thread_id='*'):
        # Yes, thread suspend is done at this point, not through an internal command.
        threads = []
        suspend_all = thread_id.strip() == '*'
        if suspend_all:
            threads = pydevd_utils.get_non_pydevd_threads()

        elif thread_id.startswith('__frame__:'):
            sys.stderr.write("Can't suspend tasklet: %s\n" % (thread_id,))

        else:
            threads = [pydevd_find_thread_by_id(thread_id)]

        for t in threads:
            if t is None:
                continue
            py_db.set_suspend(
                t,
                CMD_THREAD_SUSPEND,
                suspend_other_threads=suspend_all,
                is_pause=True,
            )
            # Break here (even if it's suspend all) as py_db.set_suspend will
            # take care of suspending other threads.
            break

    def set_enable_thread_notifications(self, py_db, enable):
        '''
        When disabled, no thread notifications (for creation/removal) will be
        issued until it's re-enabled.

        Note that when it's re-enabled, a creation notification will be sent for
        all existing threads even if it was previously sent (this is meant to
        be used on disconnect/reconnect).
        '''
        py_db.set_enable_thread_notifications(enable)

    def request_disconnect(self, py_db, resume_threads):
        self.set_enable_thread_notifications(py_db, False)
        self.remove_all_breakpoints(py_db, filename='*')
        self.remove_all_exception_breakpoints(py_db)
        self.notify_disconnect(py_db)
        if resume_threads:
            self.request_resume_thread(thread_id='*')

    def request_resume_thread(self, thread_id):
        threads = []
        if thread_id == '*':
            threads = pydevd_utils.get_non_pydevd_threads()

        elif thread_id.startswith('__frame__:'):
            sys.stderr.write("Can't make tasklet run: %s\n" % (thread_id,))

        else:
            threads = [pydevd_find_thread_by_id(thread_id)]

        for t in threads:
            if t is None:
                continue

            internal_run_thread(t, set_additional_thread_info=set_additional_thread_info)

    def request_completions(self, py_db, seq, thread_id, frame_id, act_tok, line=-1, column=-1):
        py_db.post_method_as_internal_command(
            thread_id, internal_get_completions, seq, thread_id, frame_id, act_tok, line=line, column=column)

    def request_stack(self, py_db, seq, thread_id, fmt=None, timeout=.5, start_frame=0, levels=0):
        # If it's already suspended, get it right away.
        internal_get_thread_stack = InternalGetThreadStack(
            seq, thread_id, py_db, set_additional_thread_info, fmt=fmt, timeout=timeout, start_frame=start_frame, levels=levels)
        if internal_get_thread_stack.can_be_executed_by(get_current_thread_id(threading.current_thread())):
            internal_get_thread_stack.do_it(py_db)
        else:
            py_db.post_internal_command(internal_get_thread_stack, '*')

    def request_exception_info_json(self, py_db, request, thread_id, max_frames):
        py_db.post_method_as_internal_command(
            thread_id,
            internal_get_exception_details_json,
            request,
            thread_id,
            max_frames,
            set_additional_thread_info=set_additional_thread_info,
            iter_visible_frames_info=py_db.cmd_factory._iter_visible_frames_info,
        )

    def request_step(self, py_db, thread_id, step_cmd_id):
        t = pydevd_find_thread_by_id(thread_id)
        if t:
            py_db.post_method_as_internal_command(
                thread_id,
                internal_step_in_thread,
                thread_id,
                step_cmd_id,
                set_additional_thread_info=set_additional_thread_info,
            )
        elif thread_id.startswith('__frame__:'):
            sys.stderr.write("Can't make tasklet step command: %s\n" % (thread_id,))

    def request_set_next(self, py_db, seq, thread_id, set_next_cmd_id, line, func_name):
        t = pydevd_find_thread_by_id(thread_id)
        if t:
            int_cmd = InternalSetNextStatementThread(thread_id, set_next_cmd_id, line, func_name, seq=seq)
            py_db.post_internal_command(int_cmd, thread_id)
        elif thread_id.startswith('__frame__:'):
            sys.stderr.write("Can't set next statement in tasklet: %s\n" % (thread_id,))

    def request_reload_code(self, py_db, seq, module_name):
        thread_id = '*'  # Any thread
        # Note: not going for the main thread because in this case it'd only do the load
        # when we stopped on a breakpoint.
        py_db.post_method_as_internal_command(
            thread_id, internal_reload_code, seq, module_name)

    def request_change_variable(self, py_db, seq, thread_id, frame_id, scope, attr, value):
        '''
        :param scope: 'FRAME' or 'GLOBAL'
        '''
        py_db.post_method_as_internal_command(
            thread_id, internal_change_variable, seq, thread_id, frame_id, scope, attr, value)

    def request_get_variable(self, py_db, seq, thread_id, frame_id, scope, attrs):
        '''
        :param scope: 'FRAME' or 'GLOBAL'
        '''
        int_cmd = InternalGetVariable(seq, thread_id, frame_id, scope, attrs)
        py_db.post_internal_command(int_cmd, thread_id)

    def request_get_array(self, py_db, seq, roffset, coffset, rows, cols, fmt, thread_id, frame_id, scope, attrs):
        int_cmd = InternalGetArray(seq, roffset, coffset, rows, cols, fmt, thread_id, frame_id, scope, attrs)
        py_db.post_internal_command(int_cmd, thread_id)

    def request_load_full_value(self, py_db, seq, thread_id, frame_id, vars):
        int_cmd = InternalLoadFullValue(seq, thread_id, frame_id, vars)
        py_db.post_internal_command(int_cmd, thread_id)

    def request_get_description(self, py_db, seq, thread_id, frame_id, expression):
        py_db.post_method_as_internal_command(
            thread_id, internal_get_description, seq, thread_id, frame_id, expression)

    def request_get_frame(self, py_db, seq, thread_id, frame_id):
        py_db.post_method_as_internal_command(
            thread_id, internal_get_frame, seq, thread_id, frame_id)

    def to_str(self, s):
        '''
        In py2 converts a unicode to str (bytes) using utf-8.
        -- in py3 raises an error if it's not str already.
        '''
        if s.__class__ != str:
            if not IS_PY3K:
                s = s.encode('utf-8')
            else:
                raise AssertionError('Expected to have str on Python 3. Found: %s (%s)' % (s, s.__class__))
        return s

    def filename_to_str(self, filename):
        '''
        In py2 converts a unicode to str (bytes) using the file system encoding.
        -- in py3 raises an error if it's not str already.
        '''
        if filename.__class__ != str:
            if not IS_PY3K:
                filename = filename.encode(file_system_encoding)
            else:
                raise AssertionError('Expected to have str on Python 3. Found: %s (%s)' % (filename, filename.__class__))
        return filename

    def filename_to_server(self, filename):
        filename = self.filename_to_str(filename)
        return pydevd_file_utils.norm_file_to_server(filename)

    class _DummyFrame(object):
        '''
        Dummy frame to be used with PyDB.apply_files_filter (as we don't really have the
        related frame as breakpoints are added before execution).
        '''

        class _DummyCode(object):

            def __init__(self, filename):
                self.co_firstlineno = 1
                self.co_filename = filename
                self.co_name = 'invalid func name '

        def __init__(self, filename):
            self.f_code = self._DummyCode(filename)
            self.f_globals = {}

    ADD_BREAKPOINT_NO_ERROR = 0
    ADD_BREAKPOINT_FILE_NOT_FOUND = 1
    ADD_BREAKPOINT_FILE_EXCLUDED_BY_FILTERS = 2

    def add_breakpoint(
            self, py_db, filename, breakpoint_type, breakpoint_id, line, condition, func_name, expression, suspend_policy, hit_condition, is_logpoint):
        '''
        :param str filename:
            Note: must be already translated for the server.

        :param str breakpoint_type:
            One of: 'python-line', 'django-line', 'jinja2-line'.

        :param int breakpoint_id:

        :param int line:

        :param condition:
            Either None or the condition to activate the breakpoint.

        :param str func_name:
            If "None" (str), may hit in any context.
            Empty string will hit only top level.
            Any other value must match the scope of the method to be matched.

        :param str expression:
            None or the expression to be evaluated.

        :param suspend_policy:
            Either "NONE" (to suspend only the current thread when the breakpoint is hit) or
            "ALL" (to suspend all threads when a breakpoint is hit).

        :param str hit_condition:
            An expression where `@HIT@` will be replaced by the number of hits.
            i.e.: `@HIT@ == x` or `@HIT@ >= x`

        :param bool is_logpoint:
            If True and an expression is passed, pydevd will create an io message command with the
            result of the evaluation.

        :return int:
            :see: ADD_BREAKPOINT_NO_ERROR = 0
            :see: ADD_BREAKPOINT_FILE_NOT_FOUND = 1
            :see: ADD_BREAKPOINT_FILE_EXCLUDED_BY_FILTERS = 2
        '''
        assert filename.__class__ == str  # i.e.: bytes on py2 and str on py3
        assert func_name.__class__ == str  # i.e.: bytes on py2 and str on py3

        if not pydevd_file_utils.exists(filename):
            return self.ADD_BREAKPOINT_FILE_NOT_FOUND

        error_code = self.ADD_BREAKPOINT_NO_ERROR
        if (
                py_db.is_files_filter_enabled and
                not py_db.get_require_module_for_filters() and
                py_db.apply_files_filter(self._DummyFrame(filename), filename, False)
            ):
            # Note that if `get_require_module_for_filters()` returns False, we don't do this check.
            # This is because we don't have the module name given a file at this point (in
            # runtime it's gotten from the frame.f_globals).
            # An option could be calculate it based on the filename and current sys.path,
            # but on some occasions that may be wrong (for instance with `__main__` or if
            # the user dynamically changes the PYTHONPATH).

            # Note: depending on the use-case, filters may be changed, so, keep on going and add the
            # breakpoint even with the error code.
            error_code = self.ADD_BREAKPOINT_FILE_EXCLUDED_BY_FILTERS

        if breakpoint_type == 'python-line':
            added_breakpoint = LineBreakpoint(line, condition, func_name, expression, suspend_policy, hit_condition=hit_condition, is_logpoint=is_logpoint)
            breakpoints = py_db.breakpoints
            file_to_id_to_breakpoint = py_db.file_to_id_to_line_breakpoint
            supported_type = True

        else:
            result = None
            plugin = py_db.get_plugin_lazy_init()
            if plugin is not None:
                result = plugin.add_breakpoint('add_line_breakpoint', py_db, breakpoint_type, filename, line, condition, expression, func_name, hit_condition=hit_condition, is_logpoint=is_logpoint)
            if result is not None:
                supported_type = True
                added_breakpoint, breakpoints = result
                file_to_id_to_breakpoint = py_db.file_to_id_to_plugin_breakpoint
            else:
                supported_type = False

        if not supported_type:
            raise NameError(breakpoint_type)

        if DebugInfoHolder.DEBUG_TRACE_BREAKPOINTS > 0:
            pydev_log.debug('Added breakpoint:%s - line:%s - func_name:%s\n', filename, line, func_name)

        if filename in file_to_id_to_breakpoint:
            id_to_pybreakpoint = file_to_id_to_breakpoint[filename]
        else:
            id_to_pybreakpoint = file_to_id_to_breakpoint[filename] = {}

        id_to_pybreakpoint[breakpoint_id] = added_breakpoint
        py_db.consolidate_breakpoints(filename, id_to_pybreakpoint, breakpoints)
        if py_db.plugin is not None:
            py_db.has_plugin_line_breaks = py_db.plugin.has_line_breaks()

        py_db.on_breakpoints_changed()
        return error_code

    def remove_all_breakpoints(self, py_db, filename):
        '''
        Removes all the breakpoints from a given file or from all files if filename == '*'.
        '''
        changed = False
        lst = [
            py_db.file_to_id_to_line_breakpoint,
            py_db.file_to_id_to_plugin_breakpoint,
            py_db.breakpoints
        ]
        if hasattr(py_db, 'django_breakpoints'):
            lst.append(py_db.django_breakpoints)

        if hasattr(py_db, 'jinja2_breakpoints'):
            lst.append(py_db.jinja2_breakpoints)

        for file_to_id_to_breakpoint in lst:
            if filename == '*':
                if file_to_id_to_breakpoint:
                    file_to_id_to_breakpoint.clear()
                    changed = True
            else:
                if filename in file_to_id_to_breakpoint:
                    del file_to_id_to_breakpoint[filename]
                    changed = True

        if changed:
            py_db.on_breakpoints_changed(removed=True)

    def remove_breakpoint(self, py_db, filename, breakpoint_type, breakpoint_id):
        '''
        :param str filename:
            Note: must be already translated for the server.

        :param str breakpoint_type:
            One of: 'python-line', 'django-line', 'jinja2-line'.

        :param int breakpoint_id:
        '''
        file_to_id_to_breakpoint = None

        if breakpoint_type == 'python-line':
            breakpoints = py_db.breakpoints
            file_to_id_to_breakpoint = py_db.file_to_id_to_line_breakpoint

        elif py_db.plugin is not None:
            result = py_db.plugin.get_breakpoints(py_db, breakpoint_type)
            if result is not None:
                file_to_id_to_breakpoint = py_db.file_to_id_to_plugin_breakpoint
                breakpoints = result

        if file_to_id_to_breakpoint is None:
            pydev_log.critical('Error removing breakpoint. Cannot handle breakpoint of type %s', breakpoint_type)

        else:
            try:
                id_to_pybreakpoint = file_to_id_to_breakpoint.get(filename, {})
                if DebugInfoHolder.DEBUG_TRACE_BREAKPOINTS > 0:
                    existing = id_to_pybreakpoint[breakpoint_id]
                    pydev_log.info('Removed breakpoint:%s - line:%s - func_name:%s (id: %s)\n' % (
                        filename, existing.line, existing.func_name.encode('utf-8'), breakpoint_id))

                del id_to_pybreakpoint[breakpoint_id]
                py_db.consolidate_breakpoints(filename, id_to_pybreakpoint, breakpoints)
                if py_db.plugin is not None:
                    py_db.has_plugin_line_breaks = py_db.plugin.has_line_breaks()

            except KeyError:
                pydev_log.info("Error removing breakpoint: Breakpoint id not found: %s id: %s. Available ids: %s\n",
                    filename, breakpoint_id, dict_keys(id_to_pybreakpoint))

        py_db.on_breakpoints_changed(removed=True)

    def request_exec_or_evaluate(
            self, py_db, seq, thread_id, frame_id, expression, is_exec, trim_if_too_big, attr_to_set_result):
        py_db.post_method_as_internal_command(
            thread_id, internal_evaluate_expression,
            seq, thread_id, frame_id, expression, is_exec, trim_if_too_big, attr_to_set_result)

    def request_exec_or_evaluate_json(
            self, py_db, request, thread_id):
        py_db.post_method_as_internal_command(
            thread_id, internal_evaluate_expression_json, request, thread_id)

    def request_set_expression_json(self, py_db, request, thread_id):
        py_db.post_method_as_internal_command(
            thread_id, internal_set_expression_json, request, thread_id)

    def request_console_exec(self, py_db, seq, thread_id, frame_id, expression):
        int_cmd = InternalConsoleExec(seq, thread_id, frame_id, expression)
        py_db.post_internal_command(int_cmd, thread_id)

    def request_load_source(self, py_db, seq, filename):
        '''
        :param str filename:
            Note: must be already translated for the server.
        '''
        try:
            assert filename.__class__ == str  # i.e.: bytes on py2 and str on py3

            with open(filename, 'r') as stream:
                source = stream.read()
            cmd = py_db.cmd_factory.make_load_source_message(seq, source)
        except:
            cmd = py_db.cmd_factory.make_error_message(seq, get_exception_traceback_str())

        py_db.writer.add_command(cmd)

    def add_python_exception_breakpoint(
            self,
            py_db,
            exception,
            condition,
            expression,
            notify_on_handled_exceptions,
            notify_on_unhandled_exceptions,
            notify_on_first_raise_only,
            ignore_libraries,
        ):
        exception_breakpoint = py_db.add_break_on_exception(
            exception,
            condition=condition,
            expression=expression,
            notify_on_handled_exceptions=notify_on_handled_exceptions,
            notify_on_unhandled_exceptions=notify_on_unhandled_exceptions,
            notify_on_first_raise_only=notify_on_first_raise_only,
            ignore_libraries=ignore_libraries,
        )

        if exception_breakpoint is not None:
            py_db.on_breakpoints_changed()

    def add_plugins_exception_breakpoint(self, py_db, breakpoint_type, exception):
        supported_type = False
        plugin = py_db.get_plugin_lazy_init()
        if plugin is not None:
            supported_type = plugin.add_breakpoint('add_exception_breakpoint', py_db, breakpoint_type, exception)

        if supported_type:
            py_db.has_plugin_exception_breaks = py_db.plugin.has_exception_breaks()
            py_db.on_breakpoints_changed()
        else:
            raise NameError(breakpoint_type)

    def remove_python_exception_breakpoint(self, py_db, exception):
        try:
            cp = py_db.break_on_uncaught_exceptions.copy()
            cp.pop(exception, None)
            py_db.break_on_uncaught_exceptions = cp

            cp = py_db.break_on_caught_exceptions.copy()
            cp.pop(exception, None)
            py_db.break_on_caught_exceptions = cp
        except:
            pydev_log.exception("Error while removing exception %s", sys.exc_info()[0])

        py_db.on_breakpoints_changed(removed=True)

    def remove_plugins_exception_breakpoint(self, py_db, exception_type, exception):
        # I.e.: no need to initialize lazy (if we didn't have it in the first place, we can't remove
        # anything from it anyways).
        plugin = py_db.plugin
        if plugin is None:
            return

        supported_type = plugin.remove_exception_breakpoint(py_db, exception_type, exception)

        if supported_type:
            py_db.has_plugin_exception_breaks = py_db.plugin.has_exception_breaks()
        else:
            raise NameError(exception_type)

        py_db.on_breakpoints_changed(removed=True)

    def remove_all_exception_breakpoints(self, py_db):
        py_db.break_on_uncaught_exceptions = {}
        py_db.break_on_caught_exceptions = {}

        plugin = py_db.plugin
        if plugin is not None:
            plugin.remove_all_exception_breakpoints(py_db)
        py_db.on_breakpoints_changed(removed=True)

    def set_project_roots(self, py_db, project_roots):
        '''
        :param unicode project_roots:
        '''
        py_db.set_project_roots(project_roots)

    def set_stepping_resumes_all_threads(self, py_db, stepping_resumes_all_threads):
        py_db.stepping_resumes_all_threads = stepping_resumes_all_threads

    # Add it to the namespace so that it's available as PyDevdAPI.ExcludeFilter
    from _pydevd_bundle.pydevd_filtering import ExcludeFilter  # noqa

    def set_exclude_filters(self, py_db, exclude_filters):
        '''
        :param list(PyDevdAPI.ExcludeFilter) exclude_filters:
        '''
        py_db.set_exclude_filters(exclude_filters)

    def set_use_libraries_filter(self, py_db, use_libraries_filter):
        py_db.set_use_libraries_filter(use_libraries_filter)

    def request_get_variable_json(self, py_db, request, thread_id):
        '''
        :param VariablesRequest request:
        '''
        py_db.post_method_as_internal_command(
            thread_id, internal_get_variable_json, request)

    def request_change_variable_json(self, py_db, request, thread_id):
        '''
        :param SetVariableRequest request:
        '''
        py_db.post_method_as_internal_command(
            thread_id, internal_change_variable_json, request)

    def set_dont_trace_start_end_patterns(self, py_db, start_patterns, end_patterns):
        # After it's set the first time, we can still change it, but we need to reset the
        # related caches.
        reset_caches = False
        dont_trace_start_end_patterns_previously_set = \
            py_db.dont_trace_external_files.__name__ == 'custom_dont_trace_external_files'

        if not dont_trace_start_end_patterns_previously_set and not start_patterns and not end_patterns:
            # If it wasn't set previously and start and end patterns are empty we don't need to do anything.
            return

        if not py_db.is_cache_file_type_empty():
            # i.e.: custom function set in set_dont_trace_start_end_patterns.
            if dont_trace_start_end_patterns_previously_set:
                reset_caches = py_db.dont_trace_external_files.start_patterns != start_patterns or \
                    py_db.dont_trace_external_files.end_patterns != end_patterns

            else:
                reset_caches = True

        def custom_dont_trace_external_files(abs_path):
            return abs_path.startswith(start_patterns) or abs_path.endswith(end_patterns)

        custom_dont_trace_external_files.start_patterns = start_patterns
        custom_dont_trace_external_files.end_patterns = end_patterns
        py_db.dont_trace_external_files = custom_dont_trace_external_files

        if reset_caches:
            py_db.clear_dont_trace_start_end_patterns_caches()

    def stop_on_entry(self):
        main_thread = pydevd_utils.get_main_thread()
        if main_thread is None:
            pydev_log.critical('Could not find main thread while setting Stop on Entry.')
        else:
            info = set_additional_thread_info(main_thread)
            info.pydev_original_step_cmd = CMD_STOP_ON_START
            info.pydev_step_cmd = CMD_STEP_INTO_MY_CODE

    def set_ignore_system_exit_codes(self, py_db, ignore_system_exit_codes):
        py_db.set_ignore_system_exit_codes(ignore_system_exit_codes)

