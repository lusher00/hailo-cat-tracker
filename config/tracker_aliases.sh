# tracker_aliases.sh — hailo-tracker service control, sourced from ~/.bashrc
#
# THE SPLIT. This file holds what is specific to THIS PROJECT: the service, its
# log, its config, and the repo's own tools. Everything about having a usable
# shell -- history, prompt, ls, directory sizes, git, i2c, vcgencmd, dmesg --
# lives in ~/.bashrc (installed from bashrc.pi), because it is worth having on a
# Pi with no hailo-tracker on it. The Hailo NPU helpers live there too: the HAT
# is board hardware, present whether or not this app is installed.
#
# THE RULE: no name may be defined in both files. config/install_bashrc.sh
# checks it and refuses to install on a collision, so a name can never be
# defined twice with one silently winning and the file you would read to find
# out becoming a lie.
#
# bhelp reads BOTH files, so everything still appears in one menu.
#
# This is sourced from the repo, so editing it here takes effect on the next
# shell -- no reinstall needed.

HT_DIR="${HT_DIR:-$HOME/hailo-tracker}"

#:: Hailo Tracker service
alias ht="cd $HT_DIR"   #: cd to the project

# -n 0 --no-pager on every status: `systemctl status` tails the last 10 journal
# lines by default, which for this service is a wall of detection output that
# buries the one thing you asked for -- whether it is running. Use htlog when
# you want the log.
alias hts='systemctl status -n 0 --no-pager hailo-tracker'   #: status
alias sht='sudo systemctl stop hailo-tracker'                #: stop
alias rht='sudo systemctl restart hailo-tracker'             #: restart
alias eht='sudo systemctl enable --now hailo-tracker'        #: enable at boot and start
alias dht='sudo systemctl disable --now hailo-tracker'       #: disable at boot and stop

#:: Hailo Tracker logs
alias htlog='journalctl -u hailo-tracker -f'                       #: follow the service
alias hterr='journalctl -u hailo-tracker -p warning -n 200 --no-pager'  #: last 200 warnings and errors
alias htboot='journalctl -u hailo-tracker -b --no-pager | head -60'     #: what it printed at startup

#:: Hailo Tracker config
# Edit the env file, not the .py -- every value in it overrides the CONFIG
# block, and a restart is all it takes. The edit is useless until the restart,
# so htedit does both.
alias htenv="\${EDITOR:-nano} $HT_DIR/hailo-tracker.env"   #: edit the env file
htedit() {                                                 #: edit the env file, then restart
    "${EDITOR:-nano}" "$HT_DIR/hailo-tracker.env" &&
        sudo systemctl restart hailo-tracker &&
        systemctl status -n 0 --no-pager hailo-tracker
}
# The service reads the env file through EnvironmentFile=, so what systemd
# actually loaded can differ from what the file says on disk after an edit.
alias htconf='systemctl show hailo-tracker -p Environment --no-pager'  #: env systemd actually loaded

#:: Hailo Tracker endpoints
HT_URL="${HT_URL:-http://localhost:8080}"
alias htweb="echo \"http://\$(hostname -I | awk '{print \$1}'):8080\""  #: print the web UI URL
alias htstats="curl -s $HT_URL/stats"        #: fps, detections, per-camera stats
alias httracks="curl -s $HT_URL/tracks"      #: current track list
alias htevents="curl -s $HT_URL/events"      #: recent event log rows
alias hthealth="curl -s $HT_URL/healthz"     #: liveness check
alias htmetrics="curl -s $HT_URL/metrics"    #: prometheus metrics
alias htshot="curl -s -o /tmp/snapshot.jpg $HT_URL/snapshot && echo /tmp/snapshot.jpg"  #: grab a snapshot

#:: Hailo Tracker tools
alias httest="$HT_DIR/tests/run_tests.sh"          #: off-device test harness
alias htmodel="$HT_DIR/download_model.sh"          #: fetch the .hef model
alias htsetup="$HT_DIR/install.sh"                 #: (re)install the service and udev rule
alias htkeys="python3 $HT_DIR/tools/install_keybindings.py"  #: install the vscode keybindings
alias htdb="sqlite3 $HT_DIR/events.db"             #: open the event database
alias htsnaps="ls -lt $HT_DIR/snapshots 2>/dev/null | head -20"  #: newest snapshots
