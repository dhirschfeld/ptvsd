import linecache
import os.path
import re

from _pydev_bundle import pydev_log
from _pydevd_bundle import pydevd_dont_trace
from _pydevd_bundle.pydevd_constants import (dict_iter_values, IS_PY3K, RETURN_VALUES_DICT, NO_FTRACE)
from _pydevd_bundle.pydevd_frame_utils import add_exception_to_frame, just_raised, remove_exception_from_frame, ignore_exception_trace
from _pydevd_bundle.pydevd_utils import get_clsname_for_code
from pydevd_file_utils import get_abs_path_real_path_and_base_from_frame, get_abs_path_real_path_and_base_from_file
try:
    from inspect import CO_GENERATOR
except:
    CO_GENERATOR = 0

# IFDEF CYTHON
# cython_inline_constant: CMD_STEP_INTO = 107
# cython_inline_constant: CMD_STEP_INTO_MY_CODE = 144
# cython_inline_constant: CMD_STEP_RETURN = 109
# cython_inline_constant: CMD_STEP_RETURN_MY_CODE = 160
# cython_inline_constant: CMD_STEP_OVER = 108
# cython_inline_constant: CMD_STEP_OVER_MY_CODE = 159
# cython_inline_constant: CMD_STEP_CAUGHT_EXCEPTION = 137
# cython_inline_constant: CMD_SET_BREAK = 111
# cython_inline_constant: CMD_SMART_STEP_INTO = 128
# cython_inline_constant: STATE_RUN = 1
# cython_inline_constant: STATE_SUSPEND = 2
# ELSE
# Note: those are now inlined on cython.
CMD_STEP_INTO = 107
CMD_STEP_INTO_MY_CODE = 144
CMD_STEP_RETURN = 109
CMD_STEP_RETURN_MY_CODE = 160
CMD_STEP_OVER = 108
CMD_STEP_OVER_MY_CODE = 159
CMD_STEP_CAUGHT_EXCEPTION = 137
CMD_SET_BREAK = 111
CMD_SMART_STEP_INTO = 128
STATE_RUN = 1
STATE_SUSPEND = 2
# ENDIF

try:
    from _pydevd_bundle.pydevd_signature import send_signature_call_trace, send_signature_return_trace
except ImportError:

    def send_signature_call_trace(*args, **kwargs):
        pass

basename = os.path.basename

IGNORE_EXCEPTION_TAG = re.compile('[^#]*#.*@IgnoreException')
DEBUG_START = ('pydevd.py', 'run')
DEBUG_START_PY3K = ('_pydev_execfile.py', 'execfile')
TRACE_PROPERTY = 'pydevd_traceproperty.py'


#=======================================================================================================================
# PyDBFrame
#=======================================================================================================================
# IFDEF CYTHON
# cdef class PyDBFrame:
# ELSE
class PyDBFrame:
    '''This makes the tracing for a given frame, so, the trace_dispatch
    is used initially when we enter into a new context ('call') and then
    is reused for the entire context.
    '''
# ENDIF

    # Note: class (and not instance) attributes.

    # Same thing in the main debugger but only considering the file contents, while the one in the main debugger
    # considers the user input (so, the actual result must be a join of both).
    filename_to_lines_where_exceptions_are_ignored = {}
    filename_to_stat_info = {}

    # IFDEF CYTHON
    # cdef tuple _args
    # cdef int should_skip
    # def __init__(self, tuple args):
        # self._args = args # In the cython version we don't need to pass the frame
        # self.should_skip = -1  # On cythonized version, put in instance.
    # ELSE
    should_skip = -1  # Default value in class (put in instance on set).

    def __init__(self, args):
        # args = main_debugger, filename, base, info, t, frame
        # yeap, much faster than putting in self and then getting it from self later on
        self._args = args
    # ENDIF

    def set_suspend(self, *args, **kwargs):
        self._args[0].set_suspend(*args, **kwargs)

    def do_wait_suspend(self, *args, **kwargs):
        self._args[0].do_wait_suspend(*args, **kwargs)

    # IFDEF CYTHON
    # def trace_exception(self, frame, str event, arg):
    #     cdef bint should_stop;
    # ELSE
    def trace_exception(self, frame, event, arg):
    # ENDIF
        if event == 'exception':
            should_stop, frame = self.should_stop_on_exception(frame, event, arg)

            if should_stop:
                self.handle_exception(frame, event, arg)
                return self.trace_dispatch

        return self.trace_exception

    def trace_return(self, frame, event, arg):
        if event == 'return':
            main_debugger, filename = self._args[0], self._args[1]
            send_signature_return_trace(main_debugger, frame, filename, arg)
        return self.trace_return

    # IFDEF CYTHON
    # def should_stop_on_exception(self, frame, str event, arg):
    #     cdef PyDBAdditionalThreadInfo info;
    #     cdef bint flag;
    # ELSE
    def should_stop_on_exception(self, frame, event, arg):
    # ENDIF

        # main_debugger, _filename, info, _thread = self._args
        main_debugger = self._args[0]
        info = self._args[2]
        should_stop = False

        # STATE_SUSPEND = 2
        if info.pydev_state != 2:  # and breakpoint is not None:
            exception, value, trace = arg

            if trace is not None and hasattr(trace, 'tb_next'):
                # on jython trace is None on the first event and it may not have a tb_next.

                should_stop = False
                exception_breakpoint = None
                try:
                    if main_debugger.plugin is not None:
                        result = main_debugger.plugin.exception_break(main_debugger, self, frame, self._args, arg)
                        if result:
                            should_stop, frame = result
                except:
                    pydev_log.exception()

                if not should_stop:
                    # It was not handled by any plugin, lets check exception breakpoints.
                    exception_breakpoint = main_debugger.get_exception_breakpoint(
                        exception, main_debugger.break_on_caught_exceptions)

                    if exception_breakpoint is not None:
                        if exception is SystemExit and main_debugger.ignore_system_exit_code(value):
                            return False, frame

                        if exception_breakpoint.condition is not None:
                            eval_result = main_debugger.handle_breakpoint_condition(info, exception_breakpoint, frame)
                            if not eval_result:
                                return False, frame

                        if main_debugger.exclude_exception_by_filter(exception_breakpoint, trace, False):
                            pydev_log.debug("Ignore exception %s in library %s -- (%s)" % (exception, frame.f_code.co_filename, frame.f_code.co_name))
                            return False, frame

                        if ignore_exception_trace(trace):
                            return False, frame

                        was_just_raised = just_raised(trace)
                        if was_just_raised:

                            if main_debugger.skip_on_exceptions_thrown_in_same_context:
                                # Option: Don't break if an exception is caught in the same function from which it is thrown
                                return False, frame

                        if exception_breakpoint.notify_on_first_raise_only:
                            if main_debugger.skip_on_exceptions_thrown_in_same_context:
                                # In this case we never stop if it was just raised, so, to know if it was the first we
                                # need to check if we're in the 2nd method.
                                if not was_just_raised and not just_raised(trace.tb_next):
                                    return False, frame  # I.e.: we stop only when we're at the caller of a method that throws an exception

                            else:
                                if not was_just_raised:
                                    return False, frame  # I.e.: we stop only when it was just raised

                        # If it got here we should stop.
                        should_stop = True
                        try:
                            info.pydev_message = exception_breakpoint.qname
                        except:
                            info.pydev_message = exception_breakpoint.qname.encode('utf-8')

                if should_stop:
                    # Always add exception to frame (must remove later after we proceed).
                    add_exception_to_frame(frame, (exception, value, trace))

                    if exception_breakpoint is not None and exception_breakpoint.expression is not None:
                        main_debugger.handle_breakpoint_expression(exception_breakpoint, info, frame)

        return should_stop, frame

    def handle_exception(self, frame, event, arg):
        try:
            # print('handle_exception', frame.f_lineno, frame.f_code.co_name)

            # We have 3 things in arg: exception type, description, traceback object
            trace_obj = arg[2]
            main_debugger = self._args[0]

            initial_trace_obj = trace_obj
            if trace_obj.tb_next is None and trace_obj.tb_frame is frame:
                # I.e.: tb_next should be only None in the context it was thrown (trace_obj.tb_frame is frame is just a double check).
                pass
            else:
                # Get the trace_obj from where the exception was raised...
                while trace_obj.tb_next is not None:
                    trace_obj = trace_obj.tb_next

            if main_debugger.ignore_exceptions_thrown_in_lines_with_ignore_exception:
                for check_trace_obj in (initial_trace_obj, trace_obj):
                    filename = get_abs_path_real_path_and_base_from_frame(check_trace_obj.tb_frame)[1]

                    filename_to_lines_where_exceptions_are_ignored = self.filename_to_lines_where_exceptions_are_ignored

                    lines_ignored = filename_to_lines_where_exceptions_are_ignored.get(filename)
                    if lines_ignored is None:
                        lines_ignored = filename_to_lines_where_exceptions_are_ignored[filename] = {}

                    try:
                        curr_stat = os.stat(filename)
                        curr_stat = (curr_stat.st_size, curr_stat.st_mtime)
                    except:
                        curr_stat = None

                    last_stat = self.filename_to_stat_info.get(filename)
                    if last_stat != curr_stat:
                        self.filename_to_stat_info[filename] = curr_stat
                        lines_ignored.clear()
                        try:
                            linecache.checkcache(filename)
                        except:
                            # Jython 2.1
                            linecache.checkcache()

                    from_user_input = main_debugger.filename_to_lines_where_exceptions_are_ignored.get(filename)
                    if from_user_input:
                        merged = {}
                        merged.update(lines_ignored)
                        # Override what we have with the related entries that the user entered
                        merged.update(from_user_input)
                    else:
                        merged = lines_ignored

                    exc_lineno = check_trace_obj.tb_lineno

                    # print ('lines ignored', lines_ignored)
                    # print ('user input', from_user_input)
                    # print ('merged', merged, 'curr', exc_lineno)

                    if exc_lineno not in merged:  # Note: check on merged but update lines_ignored.
                        try:
                            line = linecache.getline(filename, exc_lineno, check_trace_obj.tb_frame.f_globals)
                        except:
                            # Jython 2.1
                            line = linecache.getline(filename, exc_lineno)

                        if IGNORE_EXCEPTION_TAG.match(line) is not None:
                            lines_ignored[exc_lineno] = 1
                            return
                        else:
                            # Put in the cache saying not to ignore
                            lines_ignored[exc_lineno] = 0
                    else:
                        # Ok, dict has it already cached, so, let's check it...
                        if merged.get(exc_lineno, 0):
                            return

            thread = self._args[3]

            try:
                frame_id_to_frame = {}
                frame_id_to_frame[id(frame)] = frame
                f = trace_obj.tb_frame
                while f is not None:
                    frame_id_to_frame[id(f)] = f
                    f = f.f_back
                f = None

                main_debugger.send_caught_exception_stack(thread, arg, id(frame))
                self.set_suspend(thread, CMD_STEP_CAUGHT_EXCEPTION)
                self.do_wait_suspend(thread, frame, event, arg)
                main_debugger.send_caught_exception_stack_proceeded(thread)
            except:
                pydev_log.exception()

            main_debugger.set_trace_for_frame_and_parents(frame)
        finally:
            # Make sure the user cannot see the '__exception__' we added after we leave the suspend state.
            remove_exception_from_frame(frame)
            # Clear some local variables...
            frame = None
            trace_obj = None
            initial_trace_obj = None
            check_trace_obj = None
            f = None
            frame_id_to_frame = None
            main_debugger = None
            thread = None

    def get_func_name(self, frame):
        code_obj = frame.f_code
        func_name = code_obj.co_name
        try:
            cls_name = get_clsname_for_code(code_obj, frame)
            if cls_name is not None:
                return "%s.%s" % (cls_name, func_name)
            else:
                return func_name
        except:
            pydev_log.exception()
            return func_name

    def show_return_values(self, frame, arg):
        try:
            try:
                f_locals_back = getattr(frame.f_back, "f_locals", None)
                if f_locals_back is not None:
                    return_values_dict = f_locals_back.get(RETURN_VALUES_DICT, None)
                    if return_values_dict is None:
                        return_values_dict = {}
                        f_locals_back[RETURN_VALUES_DICT] = return_values_dict
                    name = self.get_func_name(frame)
                    return_values_dict[name] = arg
            except:
                pydev_log.exception()
        finally:
            f_locals_back = None

    def remove_return_values(self, main_debugger, frame):
        try:
            try:
                # Showing return values was turned off, we should remove them from locals dict.
                # The values can be in the current frame or in the back one
                frame.f_locals.pop(RETURN_VALUES_DICT, None)

                f_locals_back = getattr(frame.f_back, "f_locals", None)
                if f_locals_back is not None:
                    f_locals_back.pop(RETURN_VALUES_DICT, None)
            except:
                pydev_log.exception()
        finally:
            f_locals_back = None

    # IFDEF CYTHON
    # cpdef trace_dispatch(self, frame, str event, arg):
    #     cdef str filename;
    #     cdef bint is_exception_event;
    #     cdef bint has_exception_breakpoints;
    #     cdef bint can_skip;
    #     cdef PyDBAdditionalThreadInfo info;
    #     cdef int step_cmd;
    #     cdef int line;
    #     cdef bint is_line;
    #     cdef bint is_call;
    #     cdef bint is_return;
    #     cdef bint should_stop;
    #     cdef dict breakpoints_for_file;
    #     cdef str curr_func_name;
    #     cdef bint exist_result;
    #     cdef dict frame_skips_cache;
    #     cdef tuple frame_cache_key;
    #     cdef tuple line_cache_key;
    #     cdef int breakpoints_in_line_cache;
    #     cdef int breakpoints_in_frame_cache;
    #     cdef bint has_breakpoint_in_frame;
    #     cdef bint need_trace_return;
    # ELSE
    def trace_dispatch(self, frame, event, arg):
    # ENDIF
        # DEBUG = 'code_to_debug' in frame.f_code.co_filename
        main_debugger, filename, info, thread, frame_skips_cache, frame_cache_key = self._args
        # if DEBUG: print('frame trace_dispatch %s %s %s %s %s' % (frame.f_lineno, frame.f_code.co_name, frame.f_code.co_filename, event, info.pydev_step_cmd))
        try:
            info.is_tracing = True
            line = frame.f_lineno
            line_cache_key = (frame_cache_key, line)

            if main_debugger._finish_debugging_session:
                return None if event == 'call' else NO_FTRACE

            plugin_manager = main_debugger.plugin

            is_exception_event = event == 'exception'
            has_exception_breakpoints = main_debugger.break_on_caught_exceptions or main_debugger.has_plugin_exception_breaks

            if is_exception_event:
                if has_exception_breakpoints:
                    should_stop, frame = self.should_stop_on_exception(frame, event, arg)
                    if should_stop:
                        self.handle_exception(frame, event, arg)
                        return self.trace_dispatch
                is_line = False
                is_return = False
                is_call = False
            else:
                is_line = event == 'line'
                is_return = event == 'return'
                is_call = event == 'call'
                if not is_line and not is_return and not is_call:
                    # Unexpected: just keep the same trace func.
                    return self.trace_dispatch

            need_signature_trace_return = False
            if main_debugger.signature_factory is not None:
                if is_call:
                    need_signature_trace_return = send_signature_call_trace(main_debugger, frame, filename)
                elif is_return:
                    send_signature_return_trace(main_debugger, frame, filename, arg)

            stop_frame = info.pydev_step_stop
            step_cmd = info.pydev_step_cmd

            if is_exception_event:
                breakpoints_for_file = None
                if stop_frame and stop_frame is not frame and step_cmd in (CMD_STEP_OVER, CMD_STEP_OVER_MY_CODE) and \
                                arg[0] in (StopIteration, GeneratorExit) and arg[2] is None:
                    if step_cmd == CMD_STEP_OVER:
                        info.pydev_step_cmd = CMD_STEP_INTO
                    else:
                        info.pydev_step_cmd = CMD_STEP_INTO_MY_CODE
                    info.pydev_step_stop = None
            else:
                # If we are in single step mode and something causes us to exit the current frame, we need to make sure we break
                # eventually.  Force the step mode to step into and the step stop frame to None.
                # I.e.: F6 in the end of a function should stop in the next possible position (instead of forcing the user
                # to make a step in or step over at that location).
                # Note: this is especially troublesome when we're skipping code with the
                # @DontTrace comment.
                if stop_frame is frame and is_return and step_cmd in (CMD_STEP_OVER, CMD_STEP_RETURN, CMD_STEP_OVER_MY_CODE, CMD_STEP_RETURN_MY_CODE):
                    if not frame.f_code.co_flags & 0x20:  # CO_GENERATOR = 0x20 (inspect.CO_GENERATOR)
                        if step_cmd in (CMD_STEP_OVER, CMD_STEP_RETURN):
                            info.pydev_step_cmd = CMD_STEP_INTO
                        else:
                            info.pydev_step_cmd = CMD_STEP_INTO_MY_CODE
                        info.pydev_step_stop = None

                breakpoints_for_file = main_debugger.breakpoints.get(filename)

                can_skip = False

                if info.pydev_state == 1:  # STATE_RUN = 1
                    # we can skip if:
                    # - we have no stop marked
                    # - we should make a step return/step over and we're not in the current frame
                    can_skip = (step_cmd == -1 and stop_frame is None) \
                        or (step_cmd in (CMD_STEP_OVER, CMD_STEP_RETURN, CMD_STEP_OVER_MY_CODE, CMD_STEP_RETURN_MY_CODE) and stop_frame is not frame)

                    if can_skip:
                        if plugin_manager is not None and (
                                main_debugger.has_plugin_line_breaks or main_debugger.has_plugin_exception_breaks):
                            can_skip = plugin_manager.can_skip(main_debugger, frame)

                        if can_skip and main_debugger.show_return_values and info.pydev_step_cmd in (CMD_STEP_OVER, CMD_STEP_OVER_MY_CODE) and frame.f_back is info.pydev_step_stop:
                            # trace function for showing return values after step over
                            can_skip = False

                # Let's check to see if we are in a function that has a breakpoint. If we don't have a breakpoint,
                # we will return nothing for the next trace
                # also, after we hit a breakpoint and go to some other debugging state, we have to force the set trace anyway,
                # so, that's why the additional checks are there.
                if not breakpoints_for_file:
                    if can_skip:
                        if has_exception_breakpoints:
                            return self.trace_exception
                        else:
                            if need_signature_trace_return:
                                return self.trace_return
                            else:
                                return None if is_call else NO_FTRACE

                else:
                    # When cached, 0 means we don't have a breakpoint and 1 means we have.
                    if can_skip:
                        breakpoints_in_line_cache = frame_skips_cache.get(line_cache_key, -1)
                        if breakpoints_in_line_cache == 0:
                            return self.trace_dispatch

                    breakpoints_in_frame_cache = frame_skips_cache.get(frame_cache_key, -1)
                    if breakpoints_in_frame_cache != -1:
                        # Gotten from cache.
                        has_breakpoint_in_frame = breakpoints_in_frame_cache == 1

                    else:
                        has_breakpoint_in_frame = False
                        # Checks the breakpoint to see if there is a context match in some function
                        curr_func_name = frame.f_code.co_name

                        # global context is set with an empty name
                        if curr_func_name in ('?', '<module>', '<lambda>'):
                            curr_func_name = ''

                        for breakpoint in dict_iter_values(breakpoints_for_file):  # jython does not support itervalues()
                            # will match either global or some function
                            if breakpoint.func_name in ('None', curr_func_name):
                                has_breakpoint_in_frame = True
                                break

                        # Cache the value (1 or 0 or -1 for default because of cython).
                        if has_breakpoint_in_frame:
                            frame_skips_cache[frame_cache_key] = 1
                        else:
                            frame_skips_cache[frame_cache_key] = 0

                    if can_skip and not has_breakpoint_in_frame:
                        if has_exception_breakpoints:
                            return self.trace_exception
                        else:
                            if need_signature_trace_return:
                                return self.trace_return
                            else:
                                return None if is_call else NO_FTRACE

            # We may have hit a breakpoint or we are already in step mode. Either way, let's check what we should do in this frame
            # if DEBUG: print('NOT skipped: %s %s %s %s' % (frame.f_lineno, frame.f_code.co_name, event, frame.__class__.__name__))

            try:
                flag = False
                # return is not taken into account for breakpoint hit because we'd have a double-hit in this case
                # (one for the line and the other for the return).

                stop_info = {}
                breakpoint = None
                exist_result = False
                stop = False
                bp_type = None
                if not is_return and info.pydev_state != STATE_SUSPEND and breakpoints_for_file is not None and line in breakpoints_for_file:
                    breakpoint = breakpoints_for_file[line]
                    new_frame = frame
                    stop = True
                    if step_cmd in (CMD_STEP_OVER, CMD_STEP_OVER_MY_CODE) and (stop_frame is frame and is_line):
                        stop = False  # we don't stop on breakpoint if we have to stop by step-over (it will be processed later)
                elif plugin_manager is not None and main_debugger.has_plugin_line_breaks:
                    result = plugin_manager.get_breakpoint(main_debugger, self, frame, event, self._args)
                    if result:
                        exist_result = True
                        flag, breakpoint, new_frame, bp_type = result

                if breakpoint:
                    # ok, hit breakpoint, now, we have to discover if it is a conditional breakpoint
                    # lets do the conditional stuff here
                    if stop or exist_result:
                        eval_result = False
                        if breakpoint.has_condition:
                            eval_result = main_debugger.handle_breakpoint_condition(info, breakpoint, new_frame)

                        if breakpoint.expression is not None:
                            main_debugger.handle_breakpoint_expression(breakpoint, info, new_frame)
                            if breakpoint.is_logpoint and info.pydev_message is not None and len(info.pydev_message) > 0:
                                cmd = main_debugger.cmd_factory.make_io_message(info.pydev_message + os.linesep, '1')
                                main_debugger.writer.add_command(cmd)

                        if breakpoint.has_condition:
                            if not eval_result:
                                return self.trace_dispatch
                        elif breakpoint.is_logpoint:
                            return self.trace_dispatch

                    if is_call and frame.f_code.co_name in ('<module>', '<lambda>'):
                        # If we find a call for a module, it means that the module is being imported/executed for the
                        # first time. In this case we have to ignore this hit as it may later duplicated by a
                        # line event at the same place (so, if there's a module with a print() in the first line
                        # the user will hit that line twice, which is not what we want).
                        #
                        # As for lambda, as it only has a single statement, it's not interesting to trace
                        # its call and later its line event as they're usually in the same line.

                        return self.trace_dispatch

                if main_debugger.show_return_values:
                    if is_return and info.pydev_step_cmd in (CMD_STEP_OVER, CMD_STEP_OVER_MY_CODE) and frame.f_back == info.pydev_step_stop:
                        self.show_return_values(frame, arg)

                elif main_debugger.remove_return_values_flag:
                    try:
                        self.remove_return_values(main_debugger, frame)
                    finally:
                        main_debugger.remove_return_values_flag = False

                if stop:
                    self.set_suspend(
                        thread,
                        CMD_SET_BREAK,
                        suspend_other_threads=breakpoint and breakpoint.suspend_policy == "ALL",
                    )

                elif flag and plugin_manager is not None:
                    result = plugin_manager.suspend(main_debugger, thread, frame, bp_type)
                    if result:
                        frame = result

                # if thread has a suspend flag, we suspend with a busy wait
                if info.pydev_state == STATE_SUSPEND:
                    self.do_wait_suspend(thread, frame, event, arg)
                    return self.trace_dispatch
                else:
                    if not breakpoint and is_line:
                        # No stop from anyone and no breakpoint found in line (cache that).
                        frame_skips_cache[line_cache_key] = 0

            except:
                pydev_log.exception()
                raise

            # step handling. We stop when we hit the right frame
            try:
                should_skip = 0
                if pydevd_dont_trace.should_trace_hook is not None:
                    if self.should_skip == -1:
                        # I.e.: cache the result on self.should_skip (no need to evaluate the same frame multiple times).
                        # Note that on a code reload, we won't re-evaluate this because in practice, the frame.f_code
                        # Which will be handled by this frame is read-only, so, we can cache it safely.
                        if not pydevd_dont_trace.should_trace_hook(frame, filename):
                            # -1, 0, 1 to be Cython-friendly
                            should_skip = self.should_skip = 1
                        else:
                            should_skip = self.should_skip = 0
                    else:
                        should_skip = self.should_skip

                plugin_stop = False
                if should_skip:
                    stop = False

                elif step_cmd in (CMD_STEP_INTO, CMD_STEP_INTO_MY_CODE):
                    force_check_project_scope = step_cmd == CMD_STEP_INTO_MY_CODE
                    if is_line:
                        if force_check_project_scope or main_debugger.is_files_filter_enabled:
                            stop = not main_debugger.apply_files_filter(frame, frame.f_code.co_filename, force_check_project_scope)
                        else:
                            stop = True

                    elif is_return and frame.f_back is not None:
                        if main_debugger.get_file_type(
                                get_abs_path_real_path_and_base_from_frame(frame.f_back)) == main_debugger.PYDEV_FILE:
                            stop = False
                        else:
                            if force_check_project_scope or main_debugger.is_files_filter_enabled:
                                stop = not main_debugger.apply_files_filter(frame.f_back, frame.f_back.f_code.co_filename, force_check_project_scope)
                            else:
                                stop = True

                    if plugin_manager is not None:
                        result = plugin_manager.cmd_step_into(main_debugger, frame, event, self._args, stop_info, stop)
                        if result:
                            stop, plugin_stop = result

                elif step_cmd in (CMD_STEP_OVER, CMD_STEP_OVER_MY_CODE):
                    # Note: when dealing with a step over my code it's the same as a step over (the
                    # difference is that when we return from a frame in one we go to regular step
                    # into and in the other we go to a step into my code).
                    stop = stop_frame is frame and is_line
                    # Note: don't stop on a return for step over, only for line events
                    # i.e.: don't stop in: (stop_frame is frame.f_back and is_return) as we'd stop twice in that line.

                    if frame.f_code.co_flags & CO_GENERATOR:
                        if is_return:
                            stop = False

                    if plugin_manager is not None:
                        result = plugin_manager.cmd_step_over(main_debugger, frame, event, self._args, stop_info, stop)
                        if result:
                            stop, plugin_stop = result

                elif step_cmd == CMD_SMART_STEP_INTO:
                    stop = False
                    if info.pydev_smart_step_stop is frame:
                        info.pydev_func_name = '.invalid.'  # Must match the type in cython
                        info.pydev_smart_step_stop = None

                    if is_line or is_exception_event:
                        curr_func_name = frame.f_code.co_name

                        # global context is set with an empty name
                        if curr_func_name in ('?', '<module>') or curr_func_name is None:
                            curr_func_name = ''

                        if curr_func_name == info.pydev_func_name:
                            stop = True

                elif step_cmd in (CMD_STEP_RETURN, CMD_STEP_RETURN_MY_CODE):
                    stop = is_return and stop_frame is frame

                else:
                    stop = False

                if stop and step_cmd != -1 and is_return and IS_PY3K and hasattr(frame, "f_back"):
                    f_code = getattr(frame.f_back, 'f_code', None)
                    if f_code is not None:
                        if main_debugger.get_file_type(
                                get_abs_path_real_path_and_base_from_file(f_code.co_filename)) == main_debugger.PYDEV_FILE:
                            stop = False

                if plugin_stop:
                    stopped_on_plugin = plugin_manager.stop(main_debugger, frame, event, self._args, stop_info, arg, step_cmd)
                elif stop:
                    if is_line:
                        self.set_suspend(thread, step_cmd)
                        self.do_wait_suspend(thread, frame, event, arg)
                    else:  # return event
                        back = frame.f_back
                        if back is not None:
                            # When we get to the pydevd run function, the debugging has actually finished for the main thread
                            # (note that it can still go on for other threads, but for this one, we just make it finish)
                            # So, just setting it to None should be OK
                            _, back_filename, base = get_abs_path_real_path_and_base_from_frame(back)
                            if (base, back.f_code.co_name) in (DEBUG_START, DEBUG_START_PY3K):
                                back = None

                            elif base == TRACE_PROPERTY:
                                # We dont want to trace the return event of pydevd_traceproperty (custom property for debugging)
                                # if we're in a return, we want it to appear to the user in the previous frame!
                                return None if is_call else NO_FTRACE

                            elif pydevd_dont_trace.should_trace_hook is not None:
                                if not pydevd_dont_trace.should_trace_hook(back, back_filename):
                                    # In this case, we'll have to skip the previous one because it shouldn't be traced.
                                    # Also, we have to reset the tracing, because if the parent's parent (or some
                                    # other parent) has to be traced and it's not currently, we wouldn't stop where
                                    # we should anymore (so, a step in/over/return may not stop anywhere if no parent is traced).
                                    # Related test: _debugger_case17a.py
                                    main_debugger.set_trace_for_frame_and_parents(back)
                                    return None if is_call else NO_FTRACE

                        if back is not None:
                            # if we're in a return, we want it to appear to the user in the previous frame!
                            self.set_suspend(thread, step_cmd)
                            self.do_wait_suspend(thread, back, event, arg)
                        else:
                            # in jython we may not have a back frame
                            info.pydev_step_stop = None
                            info.pydev_original_step_cmd = -1
                            info.pydev_step_cmd = -1
                            info.pydev_state = STATE_RUN

            except KeyboardInterrupt:
                raise
            except:
                try:
                    pydev_log.exception()
                    info.pydev_original_step_cmd = -1
                    info.pydev_step_cmd = -1
                except:
                    return None if is_call else NO_FTRACE

            # if we are quitting, let's stop the tracing
            if not main_debugger.quitting:
                return self.trace_dispatch
            else:
                return None if is_call else NO_FTRACE
        finally:
            info.is_tracing = False

        # end trace_dispatch
