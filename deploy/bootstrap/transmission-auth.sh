#!/bin/sh
# Keeps Transmission's web login in step with the MartinFlix dashboard password.
#
# Transmission has no API for changing its own password. The dashboard writes the new password into the shared
# secret file; this loop copies it into settings.json and sends Transmission SIGHUP, which makes the daemon reload
# its settings without dropping torrents. It shares Transmission's process namespace so it can signal the daemon,
# and has no network or capabilities of its own.
set -u
secret=/run/transmission-password
settings=/config/settings.json
marker=/config/.martinflix-password-applied
username=${TRANSMISSION_USER:-admin}
failures=0

apply() {
    pids=$(pidof transmission-daemon) || return 1
    [ -s "$settings" ] || return 1
    PASSWORD=$(cat "$secret") USERNAME=$username awk '
        function quote(text,    out, i, c) {
            out = ""
            for (i = 1; i <= length(text); i++) {
                c = substr(text, i, 1)
                if (c == "\\" || c == "\"") out = out "\\"
                out = out c
            }
            return "\"" out "\""
        }
        function set(name, value) {
            comma = ($0 ~ /,[ \t]*$/) ? "," : ""
            print "    \"" name "\": " value comma
        }
        /^[ \t]*"rpc-password":/ { set("rpc-password", quote(ENVIRON["PASSWORD"])); next }
        /^[ \t]*"rpc-username":/ { set("rpc-username", quote(ENVIRON["USERNAME"])); next }
        /^[ \t]*"rpc-authentication-required":/ { set("rpc-authentication-required", "true"); next }
        { print }
    ' "$settings" > "$settings.martinflix" || return 1
    # Rewrite in place so the file keeps its owner and mode.
    cat "$settings.martinflix" > "$settings" && rm -f "$settings.martinflix" || return 1
    # shellcheck disable=SC2086
    kill -HUP $pids
}

applied=$(cat "$marker" 2>/dev/null || true)
while :; do
    current=$(md5sum < "$secret" 2>/dev/null | cut -d ' ' -f 1)
    if [ -n "$current" ] && [ "$current" != "$applied" ]; then
        if apply; then
            printf '%s' "$current" > "$marker"
            applied=$current
            failures=0
            echo "Transmission login updated."
        else
            failures=$((failures + 1))
            # After a minute without a daemon to signal, restart so Docker rejoins Transmission's current namespace.
            [ "$failures" -ge 12 ] && { echo "Transmission is not running; restarting." >&2; exit 1; }
        fi
    fi
    sleep 5
done
