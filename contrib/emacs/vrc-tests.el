;;; vrc-tests.el --- Tests for vrc.el  -*- lexical-binding: t; -*-

;;; Commentary:

;; Run them:
;;
;;   emacs -Q --batch -L contrib/emacs -l contrib/emacs/vrc-tests.el \
;;         -f ert-run-tests-batch-and-exit
;;
;; The evaluation tests need a live session and skip without one.  A fake
;; Vivado is enough and costs nothing:
;;
;;   export VRC_TEST_TCLSH=/tools/Xilinx/Vivado/2023.2/bin/unwrapped/lnx64.o/tclsh8.5
;;   vrc start emacstest --vivado "$PWD/tests/fake_vivado.sh"
;;   VRC_TEST_SESSION=emacstest emacs -Q --batch ...

;;; Code:

(require 'ert)
(require 'vrc)

(defun vrc-tests--session ()
  "Return the session to test against, or skip."
  (or (getenv "VRC_TEST_SESSION")
      (ert-skip "set VRC_TEST_SESSION to a live session")))

(defun vrc-tests--eval (session script)
  "Evaluate SCRIPT in SESSION synchronously.  Return (STATUS . TEXT)."
  (let ((result nil)
        (deadline (time-add (current-time) 30)))
    (vrc--run session script
              (lambda (status text _job) (setq result (cons status text))))
    (while (and (null result) (time-less-p (current-time) deadline))
      (accept-process-output nil 0.05))
    (or result (error "Timed out evaluating %S" script))))


;;;; Parsing Vivado's output

(defun vrc-tests--match (line)
  "Return (FILE . LINE) parsed out of LINE, or nil."
  (let ((regexp (car (alist-get 'vivado vrc-log-error-regexp-alist-alist))))
    (when (string-match regexp line)
      (cons (match-string 1 line) (string-to-number (match-string 2 line))))))

(ert-deftest vrc-test-error-regexp-plain ()
  (should (equal '("/home/you/rtl/fir.v" . 42)
                 (vrc-tests--match
                  "ERROR: [Synth 8-403] loop limit exceeded [/home/you/rtl/fir.v:42]"))))

(ert-deftest vrc-test-error-regexp-quoted-path ()
  (should (equal '("/home/you/rtl/fir.sv" . 88)
                 (vrc-tests--match
                  "CRITICAL WARNING: [Synth 8-5788] port width mismatch [\"/home/you/rtl/fir.sv\":88]"))))

(ert-deftest vrc-test-error-regexp-brackets-in-body ()
  "The location is the last bracket, not the first one Vivado prints."
  (should (equal '("/home/you/rtl/fir.v" . 17)
                 (vrc-tests--match
                  "WARNING: [Synth 8-327] inferred latch for 'y[7:0]' in always block [/home/you/rtl/fir.v:17]"))))

(ert-deftest vrc-test-error-regexp-severity ()
  "Warnings and info carry their own subexpression so `next-error' can skip them."
  (let ((regexp (car (alist-get 'vivado vrc-log-error-regexp-alist-alist))))
    (should (progn (string-match regexp "WARNING: [Synth 8-327] x [/a.v:1]")
                   (match-string 3 "WARNING: [Synth 8-327] x [/a.v:1]")))
    (should (progn (string-match regexp "INFO: [Synth 8-256] x [/a.v:1]")
                   (match-string 4 "INFO: [Synth 8-256] x [/a.v:1]")))
    (should-not (progn (string-match regexp "ERROR: [Synth 8-403] x [/a.v:1]")
                       (match-string 3 "ERROR: [Synth 8-403] x [/a.v:1]")))))

(ert-deftest vrc-test-error-regexp-ignores-locationless ()
  "A message with no file reference must not be dragged into `next-error'."
  (should-not (vrc-tests--match
               "ERROR: [Common 17-55] 'get_property' expects at least one object"))
  (should-not (vrc-tests--match "INFO: [Synth 8-7079] Multithreading enabled")))

(ert-deftest vrc-test-errorinfo-regexp ()
  (let ((regexp (car (alist-get 'tcl-errorinfo vrc-log-error-regexp-alist-alist)))
        (line "    (file \"/tmp/ooc.tcl\" line 12)"))
    (should (string-match regexp line))
    (should (equal "/tmp/ooc.tcl" (match-string 1 line)))
    (should (equal "12" (match-string 2 line)))))


;;;; Small pure things

(ert-deftest vrc-test-exit-codes ()
  "The status symbols must stay in step with the CLI's documented codes."
  (should (equal 'ok (alist-get 0 vrc--exit-status)))
  (should (equal 'tcl-error (alist-get 1 vrc--exit-status)))
  (should (equal 'no-session (alist-get 3 vrc--exit-status)))
  (should (equal 'timeout (alist-get 4 vrc--exit-status)))
  (should (equal 'died (alist-get 5 vrc--exit-status)))
  (should (equal 'detached (alist-get 130 vrc--exit-status)))
  (should (equal 'died (alist-get 99 vrc--exit-status 'died))))

(ert-deftest vrc-test-read-commands ()
  (should (equal "read_verilog [list /r/a.v /r/b.sv]\nread_vhdl [list /r/c.vhd]"
                 (vrc--read-commands '("/r/a.v" "/r/b.sv" "/r/c.vhd"))))
  (should (equal "read_vhdl [list /r/c.vhdl]" (vrc--read-commands '("/r/c.vhdl")))))

(ert-deftest vrc-test-tcl-word ()
  "A path only gets quoted when Tcl would otherwise misread it."
  (should (equal "/r/a.v" (vrc--tcl-word "/r/a.v")))
  (should (equal "\"/r/my rtl/a.v\"" (vrc--tcl-word "/r/my rtl/a.v")))
  (should (equal "\"/r/a\\{b\\}.v\"" (vrc--tcl-word "/r/a{b}.v")))
  (should (equal "\"/r/a\\$b.v\"" (vrc--tcl-word "/r/a$b.v")))
  (should (equal "\"/r/a\\[b\\].v\"" (vrc--tcl-word "/r/a[b].v"))))

(ert-deftest vrc-test-wns ()
  (should (equal 0.421 (vrc--wns "WNS=0.421 from=a to=b levels=5")))
  (should (equal -1.25 (vrc--wns "WNS=-1.25 from=a")))
  (should-not (vrc--wns "no timing here")))

(ert-deftest vrc-test-wns-delta ()
  (let ((vrc--last-wns nil))
    (should (equal "" (vrc--wns-delta "s" "WNS=0.380")))
    (should (equal " (was 0.380, +0.041)" (vrc--wns-delta "s" "WNS=0.421")))
    (should (equal "" (vrc--wns-delta "s" "WNS=0.421")))
    (should (equal " (was 0.421, -0.421)" (vrc--wns-delta "s" "WNS=0.000")))))

(ert-deftest vrc-test-session-guess-is-forgotten ()
  "A guessed name is dropped once it names nothing; a chosen one is kept."
  (with-temp-buffer
    (setq vrc-session "gone" vrc--session-auto t)
    (vrc--forget-guess "gone" (current-buffer))
    (should-not vrc-session))
  (with-temp-buffer
    ;; As `.dir-locals.el' or `vrc-select-session' would leave it.
    (setq vrc-session "baseline" vrc--session-auto nil)
    (vrc--forget-guess "baseline" (current-buffer))
    (should (equal "baseline" vrc-session)))
  (with-temp-buffer
    ;; A guess, but about some other session.
    (setq vrc-session "work" vrc--session-auto t)
    (vrc--forget-guess "baseline" (current-buffer))
    (should (equal "work" vrc-session))))

(ert-deftest vrc-test-module-at-point ()
  (with-temp-buffer
    (insert "module fir_taps #(parameter N = 8) (input clk);\nendmodule\n")
    (should (equal "fir_taps" (vrc--module-at-point))))
  (with-temp-buffer
    (insert "entity fir_taps is\nend entity;\n")
    (should (equal "fir_taps" (vrc--module-at-point))))
  (with-temp-buffer
    (insert "module first_one; endmodule\nmodule second_one; endmodule\n")
    (should (equal "second_one" (vrc--module-at-point)))))


;;;; Against a live session

(ert-deftest vrc-test-eval-simple ()
  (should (equal '(ok . "42")
                 (vrc-tests--eval (vrc-tests--session) "expr {6 * 7}"))))

(ert-deftest vrc-test-eval-multiline ()
  "The case a line-oriented REPL wrapper gets wrong, so the one that matters."
  (should (equal '(ok . "sum=6")
                 (vrc-tests--eval
                  (vrc-tests--session)
                  "set acc 0\nforeach x {1 2 3} {\n  incr acc $x\n}\nreturn \"sum=$acc\""))))

(ert-deftest vrc-test-eval-braces-and-quotes ()
  "Nothing is escaped on the way in, so nothing needs escaping here."
  (should (equal '(ok . "len=12")
                 (vrc-tests--eval
                  (vrc-tests--session)
                  "set s {a \"b\" [c] $d}\nreturn \"len=[string length $s]\""))))

(ert-deftest vrc-test-eval-non-ascii ()
  "Byte counts, not character counts, all the way through."
  (should (equal '(ok . "µs ✓ Ω")
                 (vrc-tests--eval (vrc-tests--session) "return \"µs ✓ Ω\""))))

(ert-deftest vrc-test-eval-error-keeps-session ()
  (let ((session (vrc-tests--session)))
    (let ((result (vrc-tests--eval session "no_such_command")))
      (should (equal 'tcl-error (car result)))
      (should (string-match-p "invalid command name" (cdr result))))
    (should (equal '(ok . "2") (vrc-tests--eval session "expr {1 + 1}")))))

(ert-deftest vrc-test-eval-large-result ()
  (let ((result (vrc-tests--eval (vrc-tests--session)
                                 "return [string repeat x 300000]")))
    (should (equal 'ok (car result)))
    (should (equal 300000 (length (cdr result))))))

(ert-deftest vrc-test-no-session ()
  (should (equal 'no-session
                 (car (vrc-tests--eval "no-such-session-here" "expr 1")))))

(ert-deftest vrc-test-busy-guard ()
  "A second request must be refused, not queued behind a synthesis."
  (let ((session (vrc-tests--session)))
    (vrc--run session "after 3000" #'ignore "slow thing")
    (unwind-protect
        (should-error (vrc--run session "expr 1" #'ignore) :type 'user-error)
      (when-let* ((job (vrc--busy-job session)))
        (delete-process (plist-get job :proc))))))

(provide 'vrc-tests)
;;; vrc-tests.el ends here
