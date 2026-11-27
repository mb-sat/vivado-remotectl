;;; vrc.el --- Drive a persistent Vivado session from Emacs  -*- lexical-binding: t; -*-

;; Copyright (C) 2026 MBS

;; SPDX-License-Identifier: MIT

;; Author: Hannes Klas <hannes.klas@mb-satellite.com>
;; Keywords: hardware, tools, processes
;; Package-Requires: ((emacs "28.1"))
;; URL: https://github.com/mb-sat/vivado-remotectl

;;; Commentary:

;; Send Tcl from an Emacs buffer to a Vivado session held open by `vrc',
;; watch the synthesis log in a second buffer, and jump from Vivado's error
;; messages into the RTL that caused them.
;;
;; Put this directory on the load path and require it:
;;
;;   (add-to-list 'load-path "~/git/vivado-remotectl/contrib/emacs")
;;   (require 'vrc)
;;   (add-hook 'tcl-mode-hook #'vrc-mode)
;;   (add-hook 'verilog-mode-hook #'vrc-mode)
;;
;; Everything hangs off one prefix, `C-c v' by default.  `C-c v r' evaluates
;; the region, `C-c v l' shows the session log, `C-c v z' opens a prompt.
;;
;; Two design decisions a reader will question:
;;
;; It shells out to `vrc' once per evaluation rather than speaking the V1
;; frame protocol from Lisp.  A subprocess re-resolves the session descriptor
;; every time, so restarting a session is followed for free where a cached
;; connection would talk to a closed port; the session token stays inside a
;; short-lived process instead of living in Emacs memory, where a backtrace
;; or a desktop file can leak arbitrary Tcl execution as you; and vrc's exit
;; codes already are a per-request status channel.  The cost is one Python
;; start per evaluation, tens of milliseconds against a tool whose selling
;; point is turning forty seconds into seconds.  If that ever stops being
;; true, `vrc--run' is the only function that has to change.
;;
;; The REPL buffer is not the evaluation path.  `vrc repl' reads one line at
;; a time and continues a line only when it ends in a backslash, so sending a
;; region through it would split every multi-line `foreach' into separate
;; scripts.  Region sends go through `vrc -f -', where vrc's byte framing
;; means nothing has to be escaped or joined.
;;
;; Local only.  `make-process' does not honour TRAMP, and a Vivado session on
;; another host needs an Emacs on that host.

;;; Code:

(require 'compile)
(require 'comint)
(require 'seq)
(require 'subr-x)
(require 'tcl)


;;;; Customization

(defgroup vrc nil
  "Drive a persistent Vivado session from Emacs."
  :group 'tools
  :prefix "vrc-")

(defcustom vrc-program "vrc"
  "The vrc executable."
  :type 'string)

(defcustom vrc-default-session nil
  "Session to use when a buffer does not name one.
Nil means fall back to `VIVADO_REMOTECTL_SESSION', then to the only
live session if there is exactly one."
  :type '(choice (const :tag "Work it out" nil) string))

;; A single user-reserved prefix rather than the usual run of
;; `C-c C-<letter>' bindings, because there is nothing left: verilog-mode
;; already binds C-c C-c, C-c C-r, C-c C-k and C-c C-z, and vhdl-mode binds
;; C-c C-c, C-c C-k and C-c C-v.
(defcustom vrc-keymap-prefix (kbd "C-c v")
  "Prefix key for `vrc-mode-map'.
Changing this after the mode is loaded needs a restart."
  :type 'key-sequence)

(defcustom vrc-show-log-delay 5
  "Seconds before a still-running job pops up the session log.
Nil never pops it up."
  :type '(choice (const :tag "Never" nil) integer))

(defcustom vrc-log-backlog 200
  "Lines of existing log to show when a log buffer is opened."
  :type 'integer)

(defcustom vrc-log-display-action nil
  "ACTION argument passed to `display-buffer' for log buffers."
  :type 'sexp)

(defcustom vrc-start-args nil
  "Extra arguments for `vrc start', for example (\"--version\" \"2023.2\").
Best set per project in .dir-locals.el."
  :type '(repeat string))
;;;###autoload
(put 'vrc-start-args 'safe-local-variable #'listp)

(defcustom vrc-notify-after 60
  "Seconds after which a finished job raises a desktop notification.
Nil disables notifications.  A thirty-minute synthesis usually
finishes while you are looking at something else."
  :type '(choice (const :tag "Never" nil) integer))

(defcustom vrc-echo-max-lines 1
  "Result lines to show in the echo area before deferring to the transcript."
  :type 'integer)


;;;; Session state

(defvar-local vrc-session nil
  "Name of the vrc session this buffer talks to.
Set it in .dir-locals.el to point a baseline checkout and a working
tree at their own sessions.")
;;;###autoload
(put 'vrc-session 'safe-local-variable #'stringp)

(defvar-local vrc--session-auto nil
  "Non-nil when `vrc-session' was guessed rather than chosen.
A guess is dropped when it turns out to name nothing; a name from
.dir-locals.el or `vrc-select-session' is kept, because a session that
is not running yet is still the one this buffer wants.")

(defvar vrc--jobs nil
  "Alist of session name to the job plist currently running in it.")

(defvar vrc--info-cache nil
  "Alist of session name to its `vrc info --json' fields.")

(defvar vrc--last-script nil
  "Alist of session name to the last script evaluated in it.")

(defvar vrc--last-wns nil
  "Alist of session name to the last WNS reported by it.")

(defvar vrc--eval-history nil
  "Minibuffer history for `vrc-eval-string'.")

(defun vrc--get (alist session)
  "Return SESSION's entry in ALIST."
  (alist-get session (symbol-value alist) nil nil #'equal))

(defun vrc--put (alist session value)
  "Set SESSION's entry in ALIST to VALUE, or remove it when VALUE is nil."
  (set alist
       (let ((copy (symbol-value alist)))
         (setf (alist-get session copy nil (null value) #'equal) value)
         copy)))


;;;; Talking to the CLI

(defun vrc--call (&rest args)
  "Run vrc with ARGS synchronously.  Return a cons of exit code and output."
  (with-temp-buffer
    (let ((code (apply #'call-process vrc-program nil t nil args)))
      (cons code (string-trim (buffer-string))))))

(defun vrc--json (&rest args)
  "Run vrc with ARGS and parse its output as JSON.  Signal on failure."
  (let ((result (apply #'vrc--call args)))
    (unless (zerop (car result))
      (user-error "%s" (if (string-empty-p (cdr result))
                           (format "vrc %s failed" (string-join args " "))
                         (cdr result))))
    (json-parse-string (cdr result) :object-type 'alist :null-object nil)))

(defun vrc--tcl-word (string)
  "Return STRING as one Tcl word, quoted only when it has to be.
Paths reach Vivado as Tcl, so a space or a brace in one is a syntax
error rather than a file that is not found."
  (if (string-match-p "[]\\\\\"$[{} \t\n]" string)
      (concat "\"" (replace-regexp-in-string "[]\\\\\"$[{}]" "\\\\\\&" string) "\"")
    string))

(defun vrc--sessions ()
  "Return the live sessions as an alist vector from `vrc list --json'.
This probes every session, which costs half a second each against a
busy one, so call it only when a human asked."
  (append (vrc--json "list" "--json") nil))

(defun vrc--sole-session ()
  "Return the name of the only live session, or signal listing the candidates."
  (let ((names (mapcar (lambda (s) (alist-get 'name s)) (vrc--sessions))))
    (pcase (length names)
      (0 (user-error "No vrc session is running; M-x vrc-start-session"))
      (1 (car names))
      (_ (user-error "Several sessions (%s); set `vrc-session' or M-x %s"
                     (string-join names ", ") "vrc-select-session")))))

(defun vrc--session ()
  "Return the session this buffer talks to, working it out and caching if need be."
  (or vrc-session
      vrc-default-session
      (getenv "VIVADO_REMOTECTL_SESSION")
      ;; Cache it buffer-locally: the fallback costs a `vrc list', which
      ;; probes, and doing that per keystroke of a synthesis loop is rude.
      (let ((sole (vrc--sole-session)))
        (setq vrc--session-auto t)
        (setq vrc-session sole))))

(defun vrc--forget-guess (session buffer)
  "Drop SESSION from BUFFER if the name was a guess rather than a choice.
Otherwise a buffer that latched onto the only session keeps aiming at
it after you stop it and start one under another name."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (when (and vrc--session-auto (equal vrc-session session))
        (setq vrc-session nil
              vrc--session-auto nil)
        (message "vrc: %s is gone; this buffer will work out which session again"
                 session)))))

(defun vrc--info (session)
  "Return SESSION's descriptor fields, cached.
The log path and cwd do not change while a session lives, so this is
read once rather than polled."
  (or (vrc--get 'vrc--info-cache session)
      (let ((info (vrc--json "info" "--json" session)))
        (vrc--put 'vrc--info-cache session info)
        info)))

(defun vrc--cwd (session)
  "Return SESSION's working directory as a directory name."
  (file-name-as-directory
   (or (ignore-errors (alist-get 'cwd (vrc--info session))) "~")))


;;;; The core: one evaluation

(defconst vrc--exit-status
  '((0 . ok) (1 . tcl-error) (2 . usage) (3 . no-session)
    (4 . timeout) (5 . died) (130 . detached))
  "Exit codes vrc documents, as symbols.")

(defun vrc--elapsed (job)
  "Return how long JOB has been running, as a human string."
  (let ((secs (floor (float-time (time-subtract (current-time)
                                                (plist-get job :started))))))
    (if (< secs 60)
        (format "%ds" secs)
      (format "%dm%02ds" (/ secs 60) (% secs 60)))))

(defun vrc--busy-job (session)
  "Return the job running in SESSION, if any."
  (let ((job (vrc--get 'vrc--jobs session)))
    (and job (process-live-p (plist-get job :proc)) job)))

(defun vrc--run (session script callback &optional label)
  "Evaluate SCRIPT in SESSION, then call CALLBACK with (STATUS TEXT JOB).
LABEL names the job in the log separator and the mode line.  Returns
the job plist."
  ;; Vivado's interpreter is single-threaded and the plugin serialises
  ;; hard, so a second request does not fail -- it waits in the socket
  ;; queue and runs when the synthesis finishes, which from here looks
  ;; like the key did nothing.  Refuse instead.
  (when-let* ((old (vrc--busy-job session)))
    (user-error "Session %s is busy with %s (%s); %s detaches"
                session (or (plist-get old :label) "a script")
                (vrc--elapsed old)
                (substitute-command-keys "\\[vrc-detach]")))
  (let* ((out (generate-new-buffer (format " *vrc-out %s*" session) t))
         (job (list :session session :script script :label (or label "a script")
                    :started (current-time) :out out :callback callback
                    :buffer (current-buffer)))
         ;; make-process inherits `default-directory'; a TRAMP one would try
         ;; to run vrc on the remote host and a deleted one errors outright.
         (default-directory (vrc--cwd session))
         ;; `-f FILE' decodes as UTF-8 explicitly but `-f -' goes through
         ;; sys.stdin, whose encoding comes from the locale.  Remove the
         ;; asymmetry rather than hope the locale is UTF-8.
         (process-environment (cons "PYTHONIOENCODING=utf-8" process-environment))
         (proc (make-process
                :name (format "vrc-%s" session)
                ;; No process buffer: a job nobody can kill with
                ;; `kill-buffer' is the point, since it may be a thirty
                ;; minute synthesis.  We own OUT and kill it ourselves.
                :buffer nil
                :command (list vrc-program "-s" session "--no-tail" "-f" "-")
                ;; A pty would make stderr a tty, so --tail would default
                ;; back on, and would add EOL translation on the way in.
                :connection-type 'pipe
                :coding 'utf-8-unix
                :noquery t
                :filter (lambda (_proc chunk)
                          ;; Into a buffer, not a string: a
                          ;; report_utilization -return_string on a real
                          ;; design is megabytes and repeated `concat' is
                          ;; quadratic.
                          (when (buffer-live-p out)
                            (with-current-buffer out
                              (goto-char (point-max))
                              (insert chunk))))
                :sentinel #'vrc--sentinel)))
    (plist-put job :proc proc)
    (process-put proc 'vrc-job job)
    (vrc--put 'vrc--jobs session job)
    (vrc--put 'vrc--last-script session script)
    ;; vrc reads stdin to EOF before it even connects, so this cannot
    ;; deadlock on a full pipe; without the EOF it would wait forever.
    (process-send-string proc script)
    (process-send-string proc "\n")
    (process-send-eof proc)
    (vrc--mark-log session (plist-get job :label))
    (vrc--schedule-log session)
    (vrc--tick-start)
    job))

(defun vrc--sentinel (proc _event)
  "Finish the job behind PROC once it has exited."
  (unless (process-live-p proc)
    (let* ((job (process-get proc 'vrc-job))
           (session (plist-get job :session))
           (out (plist-get job :out))
           (text (if (buffer-live-p out)
                     (with-current-buffer out (buffer-string))
                   ""))
           ;; Under --no-tail exactly one stream ever carries a payload and
           ;; the exit code says which: 0 leaves the Tcl result on stdout
           ;; with stderr silent, 1 leaves $::errorInfo on stderr with
           ;; stdout silent.  So one merged stream plus the code is a
           ;; complete status channel, and a separate :stderr pipe would
           ;; only add a drain race.
           (status (if (eq (process-status proc) 'signal)
                       'detached
                     (alist-get (process-exit-status proc)
                                vrc--exit-status 'died))))
      (vrc--put 'vrc--jobs session nil)
      (when (buffer-live-p out) (kill-buffer out))
      (vrc--tick-maybe-stop)
      ;; A session we could not reach may have been restarted on a new port.
      (when (memq status '(no-session died))
        (vrc--put 'vrc--info-cache session nil))
      (when (eq status 'no-session)
        (vrc--forget-guess session (plist-get job :buffer)))
      ;; An error signalled inside a sentinel is otherwise swallowed.
      (with-demoted-errors "vrc: %S"
        (funcall (plist-get job :callback)
                 status (string-trim-right text) job)))))

(defun vrc-detach ()
  "Stop waiting for the session's current job.
The script keeps running inside Vivado, so its result is lost unless
the script also `puts' it, in which case it is still in the log.  To
stop the script itself, run `vrc cancel' from a shell."
  (interactive)
  (let* ((session (vrc--session))
         (job (vrc--busy-job session)))
    (unless job (user-error "Nothing running in session %s" session))
    (delete-process (plist-get job :proc))
    (message "vrc: detached from %s; the script is still running, %s"
             session
             (substitute-command-keys "watch it with \\[vrc-show-log]"))))


;;;; What Vivado's messages look like

;; Used by both the log buffer and the transcript, and defined before
;; either: `define-compilation-mode' wires these into
;; `compilation-error-regexp-alist' only if the symbols are bound when
;; the macro is expanded.

(defvar vrc-log-error-regexp-alist '(vivado tcl-errorinfo)
  "Which entries of `vrc-log-error-regexp-alist-alist' to use.")

(defvar vrc-log-error-regexp-alist-alist
  ;; ERROR: [Synth 8-2715] syntax error near ';' [/home/you/rtl/foo.v:42]
  ;; CRITICAL WARNING: [Synth 8-5788] ... ["/home/you/rtl/foo.sv":88]
  ;;
  ;; The `.*' is greedy so that a message body containing its own
  ;; [brackets] -- which Vivado's do, constantly -- still yields the last
  ;; one, which is the location.  The quotes around the path are optional
  ;; because Vivado only adds them sometimes.
  '((vivado
     "^\\(?:\\(?3:CRITICAL WARNING\\|WARNING\\)\\|\\(?4:INFO\\)\\|ERROR\\): \
\\[[^]\n]+\\].*\\[\"?\\(?1:[^]\"\n]+?\\)\"?:\\(?2:[0-9]+\\)\\][ \t]*$"
     1 2 nil (3 . 4))
    ;; Tcl's own traceback, so a failing `source' is navigable too.
    (tcl-errorinfo
     "^[ \t]*(file \"\\(?1:[^\"\n]+\\)\" line \\(?2:[0-9]+\\))" 1 2))
  "How to find a source location in Vivado's output.")


;;;; Reporting a result

(defun vrc-transcript-buffer (session)
  "Return SESSION's transcript buffer, creating it if need be."
  (let ((buf (get-buffer-create (format "*vrc: %s*" session))))
    (with-current-buffer buf
      (unless (derived-mode-p 'vrc-transcript-mode)
        (vrc-transcript-mode)
        (setq vrc-session session)))
    buf))

(define-compilation-mode vrc-transcript-mode "Vrc"
  "What was sent to a vrc session and what came back.
A Tcl traceback names the file and line it failed at, so a `source'
that raised is navigable with \\[next-error] like anything in the log."
  (setq-local compilation-error-regexp-alist vrc-log-error-regexp-alist)
  (setq-local compilation-error-regexp-alist-alist
              vrc-log-error-regexp-alist-alist)
  (setq-local window-point-insertion-type t))

(defun vrc--record (session script status text)
  "Append SCRIPT and its STATUS and TEXT to SESSION's transcript."
  (with-current-buffer (vrc-transcript-buffer session)
    (let ((inhibit-read-only t)
          (follow (= (point) (point-max))))
      (save-excursion
        (goto-char (point-max))
        (unless (bolp) (insert "\n"))
        (insert (format-time-string ";; %H:%M:%S ") (symbol-name status) "\n"
                (string-trim-right script) "\n")
        (unless (string-empty-p text)
          (insert (replace-regexp-in-string "^" "  " text) "\n")))
      (when follow (goto-char (point-max))))))

(defun vrc--first-lines (text n)
  "Return the first N lines of TEXT."
  (string-join (seq-take (split-string text "\n") n) "\n"))

(defun vrc--wns (text)
  "Return the WNS value in TEXT as a number, or nil."
  (when (string-match "WNS=\\([-+0-9.]+\\)" text)
    (string-to-number (match-string 1 text))))

(defun vrc--wns-delta (session text)
  "Return a \" (was X, +Y)\" suffix comparing TEXT's WNS to SESSION's last.
Remembers the new value.  Comparing two numbers is what the tool is
for, so Emacs may as well do the subtraction."
  (let ((wns (vrc--wns text))
        (previous (vrc--get 'vrc--last-wns session)))
    (when wns (vrc--put 'vrc--last-wns session wns))
    (if (and wns previous (/= wns previous))
        (format " (was %.3f, %+.3f)" previous (- wns previous))
      "")))

(defun vrc--notify (session status text elapsed)
  "Raise a desktop notification for SESSION finishing with STATUS and TEXT.
ELAPSED is how long it took."
  (when (and vrc-notify-after (>= elapsed vrc-notify-after))
    (let ((title (format "vrc %s: %s (%ds)" session status elapsed))
          (body (vrc--first-lines text 3)))
      (if (and (require 'notifications nil t) (fboundp 'notifications-notify))
          (ignore-errors (notifications-notify :title title :body body))
        (message "%s %s" title body)))))

(defun vrc--report (status text job)
  "Default callback: record and announce STATUS and TEXT for JOB."
  (let* ((session (plist-get job :session))
         (elapsed (floor (float-time (time-subtract (current-time)
                                                    (plist-get job :started))))))
    (vrc--record session (plist-get job :script) status text)
    (pcase status
      ('ok
       (let ((delta (vrc--wns-delta session text)))
         (cond
          ((string-empty-p text)
           (message "vrc: %s ok (%ds)" session elapsed))
          ((<= (length (split-string text "\n")) vrc-echo-max-lines)
           (message "%s%s" text delta))
          (t
           (message "%s%s  [full result in *vrc: %s*]"
                    (vrc--first-lines text vrc-echo-max-lines) delta session)))))
      ('tcl-error
       (message "vrc: %s raised: %s" session (vrc--first-lines text 1)))
      ('detached
       (message "vrc: detached from %s" session))
      (_
       (message "vrc: %s" (if (string-empty-p text)
                              (format "%s in session %s" status session)
                            text))))
    (vrc--notify session status text elapsed)))


;;;; Mode line

(defvar vrc--tick-timer nil)

(defun vrc--tick-start ()
  "Start refreshing the mode line while jobs are running."
  (unless vrc--tick-timer
    (setq vrc--tick-timer
          (run-at-time 1 1 (lambda () (force-mode-line-update t))))))

(defun vrc--tick-maybe-stop ()
  "Stop refreshing the mode line once no job is left."
  (when (and vrc--tick-timer (null vrc--jobs))
    (cancel-timer vrc--tick-timer)
    (setq vrc--tick-timer nil)
    (force-mode-line-update t)))

(defun vrc--lighter ()
  "Mode-line lighter naming the running job, if any.
Built from what Emacs already knows, never from a status probe:
`vrc list' opens a real connection and waits half a second, which is
not something to do on a timer."
  (let ((job (and vrc-session (vrc--busy-job vrc-session))))
    (if job
        (format " vrc[%s %s]" (plist-get job :label) (vrc--elapsed job))
      " vrc")))


;;;; Evaluating

(defun vrc--eval (script &optional label)
  "Evaluate SCRIPT in this buffer's session.  LABEL names it in the log."
  (let ((script (string-trim script)))
    (when (string-empty-p script) (user-error "Nothing to evaluate"))
    (vrc--run (vrc--session) script #'vrc--report label)))

;;;###autoload
(defun vrc-eval-region (start end)
  "Evaluate the region between START and END in this buffer's session."
  (interactive "r")
  (vrc--eval (buffer-substring-no-properties start end)
             (format "region of %s" (buffer-name))))

;;;###autoload
(defun vrc-eval-buffer ()
  "Evaluate the whole buffer in this buffer's session."
  (interactive)
  (vrc--eval (buffer-substring-no-properties (point-min) (point-max))
             (buffer-name)))

;;;###autoload
(defun vrc-eval-defun ()
  "Evaluate the top-level Tcl command around point."
  (interactive)
  (save-excursion
    (end-of-defun)
    (let ((end (point)))
      (beginning-of-defun)
      (vrc-eval-region (point) end))))

;;;###autoload
(defun vrc-eval-string (script)
  "Read SCRIPT from the minibuffer and evaluate it."
  (interactive
   (list (read-from-minibuffer (format "%s> " (vrc--session))
                               nil nil nil 'vrc--eval-history)))
  (vrc--eval script (vrc--first-lines script 1)))

;;;###autoload
(defun vrc-send-file (file)
  "Evaluate FILE in this buffer's session.
An unmodified visited file is passed as a path so Tcl's errorInfo
names it; anything else goes through stdin."
  (interactive (list (or (and buffer-file-name (not (buffer-modified-p))
                              buffer-file-name)
                         (read-file-name "Evaluate file: " nil nil t))))
  (let ((session (vrc--session)))
    ;; `source', not `vrc -f FILE': Vivado reads the file itself, so a
    ;; failure names it in errorInfo and `vrc-log-mode' can navigate there.
    (vrc--run session
              (format "source %s" (vrc--tcl-word (expand-file-name file)))
              #'vrc--report (file-name-nondirectory file))))

;;;###autoload
(defun vrc-repeat ()
  "Evaluate the last script sent to this buffer's session again."
  (interactive)
  (let* ((session (vrc--session))
         (script (vrc--get 'vrc--last-script session)))
    (unless script (user-error "Nothing has been sent to %s yet" session))
    (vrc--run session script #'vrc--report "repeat")))

;;;###autoload
(defun vrc-save-and-repeat ()
  "Save modified buffers, then run the last script again.
This is the daily loop: edit the RTL, one key, read the WNS."
  (interactive)
  (save-some-buffers t)
  (vrc-repeat))


;;;; Following the session log

;; Vivado's own output never crosses the socket.  `puts', synthesis
;; progress and every INFO/WARNING/ERROR message go to the session log;
;; only the Tcl result string comes back in the response frame.  So the log
;; is the only place to watch a run, and it is worth a buffer of its own.

(define-compilation-mode vrc-log-mode "Vivado-Log"
  "Follow a vrc session log.
Vivado messages carrying a source location are navigable with
\\[next-error].  INFO and WARNING lines stay clickable but are skipped
by \\[next-error], which visits errors only."
  (setq-local compilation-skip-threshold 2)
  (setq-local window-point-insertion-type t)
  (setq-local revert-buffer-function #'vrc--log-revert))

(defun vrc-log-buffer-name (session)
  "Return the name of SESSION's log buffer."
  (format "*vrc-log: %s*" session))

(defun vrc--log-filter (proc chunk)
  "Append CHUNK to PROC's buffer, following along where someone is watching."
  (let ((buf (process-buffer proc)))
    (when (buffer-live-p buf)
      (with-current-buffer buf
        ;; Work out who was parked at the end before inserting, so a window
        ;; scrolled back to read an error is not yanked to the bottom on
        ;; the next line of output.
        (let* ((end (point-max))
               (at-end (= (point) end))
               (following (seq-filter (lambda (w) (>= (window-point w) end))
                                      (get-buffer-window-list buf nil t)))
               (inhibit-read-only t))
          (save-excursion
            (goto-char (point-max))
            (insert chunk))
          (when at-end (goto-char (point-max)))
          (dolist (w following) (set-window-point w (point-max))))))))

(defun vrc--log-sentinel (proc _event)
  "Note in PROC's buffer that the follow has stopped."
  (unless (process-live-p proc)
    (let ((buf (process-buffer proc)))
      (when (buffer-live-p buf)
        (with-current-buffer buf
          (let ((inhibit-read-only t))
            (save-excursion
              (goto-char (point-max))
              (unless (bolp) (insert "\n"))
              (insert (format "--- vrc: no longer following %s; g reattaches ---\n"
                              vrc-session)))))))))

(defun vrc--log-revert (&rest _)
  "Reattach this log buffer to its session."
  (vrc-show-log vrc-session))

;;;###autoload
(defun vrc-show-log (&optional session)
  "Display SESSION's log in a buffer that follows new output.
Fed by `vrc log -f', which is a quarter of a second behind Vivado and
loads only `vrc-log-backlog' lines of history.  Visiting the log file
and turning on `auto-revert-tail-mode' would be 2.5 seconds behind,
would read the whole file, and would leave a file-visiting buffer that
a stray \\[save-buffer] can write back over the live log."
  (interactive)
  (let* ((session (or session (vrc--session)))
         (buf (get-buffer-create (vrc-log-buffer-name session))))
    (unless (process-live-p (get-buffer-process buf))
      (with-current-buffer buf
        (let ((inhibit-read-only t)) (erase-buffer))
        ;; Before the mode: `compilation-setup' derives
        ;; `comint-file-name-prefix' from it, and Vivado prints some paths
        ;; relative to the session's working directory.
        (setq-local default-directory (vrc--cwd session))
        (vrc-log-mode)
        (setq-local default-directory (vrc--cwd session))
        (setq vrc-session session)
        (let ((process-environment
               ;; cmd_log prints the backlog before it starts its tailer
               ;; thread, and Python block-buffers a pipe, so without this
               ;; those first lines sit unflushed until 8 kB of new output
               ;; turns up.
               (cons "PYTHONUNBUFFERED=1" process-environment)))
          (make-process
           :name (format "vrc-log-%s" session)
           ;; With a buffer, so killing the buffer stops the follow.
           :buffer buf
           ;; Under `log', -f is follow as in tail -f.  At the top level it
           ;; is file.  That is the one inconsistency in the CLI.
           :command (list vrc-program "log" "-f"
                          "-n" (number-to-string vrc-log-backlog) session)
           :connection-type 'pipe
           :coding 'utf-8-unix
           :noquery t
           :filter #'vrc--log-filter
           :sentinel #'vrc--log-sentinel))))
    (display-buffer buf vrc-log-display-action)
    buf))

(defun vrc--mark-log (session label)
  "Mark the start of LABEL in SESSION's log buffer, if it is open.
Scopes \\[next-error] to this run rather than to everything an
eight-hour session has said."
  (when-let* ((buf (get-buffer (vrc-log-buffer-name session))))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (save-excursion
          (goto-char (point-max))
          (unless (bolp) (insert "\n"))
          (insert (format-time-string "--- vrc run %H:%M:%S: ") label " ---\n"))
        (setq-local compilation-messages-start (point-max))))))

(defun vrc--schedule-log (session)
  "Show SESSION's log if its job is still running after `vrc-show-log-delay'."
  (when vrc-show-log-delay
    (let ((job (vrc--get 'vrc--jobs session)))
      (run-at-time vrc-show-log-delay nil
                   (lambda ()
                     (when (eq job (vrc--busy-job session))
                       (save-window-excursion (vrc-show-log session))
                       (display-buffer (vrc-log-buffer-name session)
                                       vrc-log-display-action)))))))


;;;; The prompt

;; For one-liners only.  Region sends never go through here: `vrc repl'
;; reads one line at a time and continues a line only when it ends in a
;; backslash, so it would split a multi-line `foreach' into separate
;; scripts.  They go through `vrc--run' instead.

(defvar-local vrc--repl-pending nil
  "Job plist for the command typed at this REPL's prompt, if one is running.")

(defvar vrc-repl-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "C-c C-c") #'vrc-repl-interrupt)
    map)
  "Keymap for `vrc-repl-mode'.")

(define-derived-mode vrc-repl-mode comint-mode "Vrc-REPL"
  "Talk to a vrc session at a prompt."
  (setq-local comint-prompt-regexp "^\\(?:[A-Za-z0-9._-]+> \\|\\.\\.\\. \\)")
  (setq-local comint-prompt-read-only t)
  (setq-local comint-input-ignoredups t)
  (setq mode-line-process '(":%s"))
  ;; The one genuinely reusable part of tcl.el.
  (set-syntax-table tcl-mode-syntax-table)
  (setq-local font-lock-defaults '(tcl-font-lock-keywords nil nil nil beginning-of-defun))
  (add-hook 'comint-input-filter-functions #'vrc--repl-input nil t)
  (add-hook 'comint-output-filter-functions #'vrc--repl-output nil t))

(defun vrc--repl-input (input)
  "Mark this REPL's session busy now that INPUT has been sent."
  (unless (string-empty-p (string-trim input))
    (setq vrc--repl-pending
          (list :session vrc-session :label "the prompt"
                :started (current-time)
                :proc (get-buffer-process (current-buffer))))
    ;; So an eval from an RTL buffer refuses instead of queueing behind
    ;; whatever was typed here.
    (vrc--put 'vrc--jobs vrc-session vrc--repl-pending)
    (vrc--tick-start)))

(defun vrc--repl-output (string)
  "Clear the busy mark when STRING brings the prompt back."
  (when (and vrc--repl-pending (string-match-p comint-prompt-regexp string))
    (vrc--put 'vrc--jobs vrc-session nil)
    (setq vrc--repl-pending nil)
    (vrc--tick-maybe-stop)))

(defun vrc-repl-interrupt ()
  "Interrupt the REPL, after saying what that actually costs.
Comint would send SIGINT, and vrc quits the REPL on one: the request
was interrupted mid-read, so the connection is out of step and reusing
it would hand the next command a stale answer.  The script itself
keeps running inside Vivado either way."
  (interactive)
  (if (null vrc--repl-pending)
      (message "vrc: nothing running at this prompt")
    (when (yes-or-no-p
           (format "%s is still running in %s; interrupting quits this REPL.  Quit? "
                   (vrc--elapsed vrc--repl-pending) vrc-session))
      (comint-interrupt-subjob))))

;;;###autoload
(defun vrc-repl (&optional session)
  "Open a prompt on SESSION."
  (interactive)
  (let* ((session (or session (vrc--session)))
         (name (format "*vrc-repl: %s*" session))
         (buf (get-buffer-create name)))
    (unless (comint-check-proc buf)
      (let ((process-environment (cons "PYTHONUNBUFFERED=1" process-environment))
            (default-directory (vrc--cwd session)))
        (make-comint-in-buffer (format "vrc-repl %s" session) buf
                               vrc-program nil "repl" session))
      (with-current-buffer buf
        (vrc-repl-mode)
        (setq vrc-session session)))
    (pop-to-buffer buf)))

;;;###autoload
(defun vrc-switch-to-repl ()
  "Go to this buffer's session prompt, starting it if need be."
  (interactive)
  (vrc-repl (vrc--session)))


;;;; Managing sessions

;;;###autoload
(defun vrc-select-session (session)
  "Point this buffer at SESSION."
  (interactive
   (list (completing-read "Session: "
                          (mapcar (lambda (s) (alist-get 'name s)) (vrc--sessions))
                          nil nil nil nil vrc-session)))
  (setq vrc-session session
        vrc--session-auto nil)
  (message "vrc: this buffer now talks to %s" session))

;;;###autoload
(defun vrc-start-session (name &optional args)
  "Start a session called NAME in `default-directory'.
ARGS are extra arguments for `vrc start'; a prefix argument prompts
for them, and `vrc-start-args' holds any this project always wants.
Vivado takes about forty seconds to come up, so this does not block,
and the buffer it was run from is pointed at the session once it is up.

The working directory matters: it is where Vivado resolves a relative
path, so run this from a buffer in the tree you mean."
  (interactive
   (list (read-string "New session name: " nil nil "default")
         (when current-prefix-arg
           (split-string (read-string "Extra `vrc start' arguments: ") nil t))))
  (let ((dir default-directory)
        (origin (current-buffer)))
    (make-process
     :name (format "vrc-start-%s" name)
     :buffer (get-buffer-create (format "*vrc-start: %s*" name))
     :command (append (list vrc-program "start" name "--cwd" (expand-file-name dir))
                      vrc-start-args args)
     :connection-type 'pipe
     :noquery t
     :sentinel
     (lambda (proc _event)
       (unless (process-live-p proc)
         (let ((code (process-exit-status proc))
               (text (with-current-buffer (process-buffer proc)
                       (string-trim (buffer-string)))))
           (if (zerop code)
               (progn
                 (vrc--put 'vrc--info-cache name nil)
                 ;; Starting a session is as clear a statement of which one
                 ;; you meant as picking it from a list.
                 (when (buffer-live-p origin)
                   (with-current-buffer origin
                     (setq vrc-session name
                           vrc--session-auto nil)))
                 (message "vrc: %s" (vrc--first-lines text 1)))
             (message "vrc: could not start %s: %s" name text)
             (display-buffer (process-buffer proc)))))))
    (message "vrc: starting %s in %s, this takes about forty seconds" name dir)))

;;;###autoload
(defun vrc-stop-session (&optional session)
  "Shut SESSION down.
Picking it from the list is the confirmation; the only extra question
is for a session with work in it, which stopping would throw away."
  (interactive (list (completing-read
                      "Stop session: "
                      (mapcar (lambda (s) (alist-get 'name s)) (vrc--sessions))
                      nil t nil nil (ignore-errors (vrc--session)))))
  (let ((session (or session (vrc--session))))
    (when (or (not (vrc--busy-job session))
              (yes-or-no-p (format "%s is running %s; stop it anyway? "
                                   session
                                   (plist-get (vrc--busy-job session) :label))))
      (vrc--put 'vrc--info-cache session nil)
      (message "vrc: %s" (cdr (vrc--call "stop" session))))))

;;;###autoload
(defun vrc-list-sessions ()
  "Show what is running."
  (interactive)
  (message "%s" (cdr (vrc--call "list"))))


;;;; Synthesising the module at point out of context

(require 'format-spec)

(defcustom vrc-ooc-part nil
  "Part to synthesise against, for example \"xcvc1902-vsva2197-2MP-e-S\".
Best set per project in .dir-locals.el."
  :type '(choice (const :tag "Ask" nil) string))
;;;###autoload
(put 'vrc-ooc-part 'safe-local-variable #'stringp)

(defcustom vrc-ooc-xdc nil
  "Constraints file read before synthesis, or nil for none."
  :type '(choice (const :tag "None" nil) file))
;;;###autoload
(put 'vrc-ooc-xdc 'safe-local-variable #'stringp)

(defcustom vrc-ooc-sources nil
  "Source files to read, or nil for the file the command is invoked from."
  :type '(choice (const :tag "This file" nil) (repeat file)))
;;;###autoload
(put 'vrc-ooc-sources 'safe-local-variable #'listp)

;; `close_project', not just `close_design'.  Files read with `read_verilog'
;; land in an implicit in-memory project and stay there, so in a session
;; that has run this before, the previous file still defines the module and
;; wins.  Vivado says so -- "overwriting previous definition of module" --
;; but as a critical warning in a log nobody is reading, and the run returns
;; the old design's timing.
(defcustom vrc-ooc-template "\
close_design -quiet
close_project -quiet
%r
%x
synth_design -top %t -part %p -mode out_of_context %g
set p [get_timing_paths -max_paths 1 -delay_type max]
return \"WNS=[get_property SLACK $p] \
from=[get_property STARTPOINT_PIN $p] \
to=[get_property ENDPOINT_PIN $p] \
levels=[get_property LOGIC_LEVELS $p]\""
  "Tcl run by `vrc-synth-ooc'.
%t is the top module, %p the part, %r the read commands, %x the
read_xdc line or nothing, %g the -generic arguments or nothing.  An
unusual flow is a one-variable override rather than a patch."
  :type 'string)
;;;###autoload
(put 'vrc-ooc-template 'safe-local-variable #'stringp)

(defun vrc--module-at-point ()
  "Return the name of the module or entity around point.
Falls back to the file name, which is right often enough to be worth
not asking."
  (or (save-excursion
        (when (re-search-backward
               "^[ \t]*\\(?:module\\|entity\\)[ \t]+\\([A-Za-z_][A-Za-z0-9_$]*\\)"
               nil t)
          (match-string-no-properties 1)))
      (and buffer-file-name (file-name-base buffer-file-name))
      (user-error "No module or entity found, and this buffer has no file")))

(defun vrc--read-commands (files)
  "Return the Tcl that reads FILES, split by language."
  (let ((vhdl (seq-filter (lambda (f) (member (file-name-extension f)
                                              '("vhd" "vhdl")))
                          files))
        (verilog (seq-remove (lambda (f) (member (file-name-extension f)
                                                 '("vhd" "vhdl")))
                             files)))
    (string-join
     (delq nil
           (list (and verilog (format "read_verilog [list %s]"
                                      (mapconcat #'vrc--tcl-word verilog " ")))
                 (and vhdl (format "read_vhdl [list %s]"
                                   (mapconcat #'vrc--tcl-word vhdl " ")))))
     "\n")))

;;;###autoload
(defun vrc-synth-ooc (&optional generics)
  "Synthesise the module at point out of context and report its WNS.
With a prefix argument, prompt for GENERICS as `NAME=VALUE ...', which
is how a parameter sweep is run without touching the packaging Tcl."
  (interactive
   (list (when current-prefix-arg
           (read-string "Generics (NAME=VALUE ...): "))))
  (let* ((top (vrc--module-at-point))
         (part (or vrc-ooc-part
                   (setq vrc-ooc-part (read-string "Part: "))))
         (files (mapcar #'expand-file-name
                        (or vrc-ooc-sources
                            (list (or buffer-file-name
                                      (user-error "This buffer has no file"))))))
         (script
          (format-spec
           vrc-ooc-template
           `((?t . ,top)
             (?p . ,part)
             (?r . ,(vrc--read-commands files))
             (?x . ,(if vrc-ooc-xdc
                        (format "read_xdc %s"
                                (vrc--tcl-word (expand-file-name vrc-ooc-xdc)))
                      ""))
             (?g . ,(mapconcat (lambda (g) (concat "-generic " g))
                               (split-string (or generics "") nil t)
                               " ")))))
         ;; An empty %x or %g leaves a blank line or a trailing space; tidy
         ;; them so the transcript reads like something a human wrote.
         (script (replace-regexp-in-string
                  "\n\n+" "\n"
                  (replace-regexp-in-string "[ \t]+$" "" script))))
    (save-some-buffers t)
    (vrc--eval script (format "synth %s" top))))


;;;; The minor mode

(defvar vrc-command-map
  (let ((map (make-sparse-keymap)))
    (define-key map "r" #'vrc-eval-region)
    (define-key map "b" #'vrc-eval-buffer)
    (define-key map "e" #'vrc-eval-string)
    (define-key map "f" #'vrc-send-file)
    (define-key map "v" #'vrc-eval-defun)
    (define-key map "g" #'vrc-save-and-repeat)
    (define-key map "l" #'vrc-show-log)
    (define-key map "t" #'vrc-transcript)
    (define-key map "z" #'vrc-switch-to-repl)
    (define-key map "k" #'vrc-detach)
    (define-key map "s" #'vrc-select-session)
    (define-key map "S" #'vrc-start-session)
    (define-key map "Q" #'vrc-stop-session)
    (define-key map "L" #'vrc-list-sessions)
    (define-key map "o" #'vrc-synth-ooc)
    map)
  "Commands under `vrc-keymap-prefix'.")

(defvar vrc-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map vrc-keymap-prefix vrc-command-map)
    map)
  "Keymap for `vrc-mode'.")

(defun vrc-transcript ()
  "Show what has been sent to this buffer's session and what came back."
  (interactive)
  (pop-to-buffer (vrc-transcript-buffer (vrc--session))))

;;;###autoload
(define-minor-mode vrc-mode
  "Send Tcl from this buffer to a Vivado session held open by vrc.

\\{vrc-mode-map}"
  :lighter (:eval (vrc--lighter))
  :keymap vrc-mode-map
  (when (and vrc-mode (derived-mode-p 'tcl-mode))
    ;; Nothing collides in tcl-mode, so the bindings tcl.el taught the
    ;; fingers work too.
    (local-set-key (kbd "C-c C-r") #'vrc-eval-region)
    (local-set-key (kbd "C-c C-x") #'vrc-eval-defun)
    (local-set-key (kbd "C-c C-z") #'vrc-switch-to-repl)))

(defun vrc--confirm-kill-emacs ()
  "Ask before leaving jobs behind, naming them.
Emacs' own \"Active processes exist\" prompt says nothing about which
session is half an hour into a synthesis."
  (or (null vrc--jobs)
      (yes-or-no-p
       (format "Jobs still running (%s); they keep going without Emacs.  Exit? "
               (mapconcat (lambda (entry)
                            (format "%s: %s %s" (car entry)
                                    (plist-get (cdr entry) :label)
                                    (vrc--elapsed (cdr entry))))
                          vrc--jobs "; ")))))

(add-hook 'kill-emacs-query-functions #'vrc--confirm-kill-emacs)

(provide 'vrc)
;;; vrc.el ends here
