#!/bin/sh
# Wiki Hub drive account agent for Samba (POSIX sh; BusyBox compatible).
#
# Applies the application's desired device accounts from accounts.tsv in the
# shared control directory. It creates person groups and device users, imports
# one-time passwords, disables or removes accounts and disconnects disabled or
# removed devices. Wiki content and its permissions are owned by the application;
# this agent never reads or changes them.
set -u
CONTROL=${WIKI_DRIVE_CONTROL:-/drive-control}
# Write reports only where the application cannot create links: a root-owned directory.
OBSERVED=${WIKI_DRIVE_OBSERVED_DIR:-$CONTROL}
SMB_CONF=${WIKI_DRIVE_SMB_CONF:-/etc/samba/smb.conf}
INTERVAL=${WIKI_DRIVE_INTERVAL:-2}
PASSWD_FILE=${WIKI_DRIVE_PASSWD_FILE:-/etc/passwd}
GROUP_FILE=${WIKI_DRIVE_GROUP_FILE:-/etc/group}
DRIVE_GROUP=wikidrive
DRIVE_GID=699999
WORK=$(mktemp -d "${TMPDIR:-/tmp}/drive-agent.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT
APPLIED=-1

log() { printf 'drive-agent: %s\n' "$*" >&2; }

group_gid() { awk -F: -v n="$1" '$1 == n { print $3 }' "$GROUP_FILE"; }
group_name() { awk -F: -v g="$1" '$3 == g { print $1; exit }' "$GROUP_FILE"; }
user_ids() { awk -F: -v n="$1" '$1 == n { print $3 ":" $4 }' "$PASSWD_FILE"; }
in_group() {
    awk -F: -v g="$2" -v u="$1" '$1 == g { n = split($4, m, ","); for (i = 1; i <= n; i++) if (m[i] == u) found = 1 }
        END { exit !found }' "$GROUP_FILE"
}

ensure_group() {
    existing=$(group_gid "$1")
    if [ -z "$existing" ]; then
        addgroup -g "$2" "$1" >/dev/null
    elif [ "$existing" != "$2" ]; then
        log "group $1 has an unexpected GID; refusing to reuse it"
        return 1
    fi
}

ensure_user() {
    existing=$(user_ids "$1")
    if [ -z "$existing" ]; then
        primary=$(group_name "$3")
        [ -n "$primary" ] || { log "missing person group for $1"; return 1; }
        adduser -D -H -u "$2" -G "$primary" -s /sbin/nologin "$1" >/dev/null || return 1
    elif [ "$existing" != "$2:$3" ]; then
        log "account $1 has unexpected IDs; refusing to reuse it"
        return 1
    fi
    in_group "$1" "$DRIVE_GROUP" || addgroup "$1" "$DRIVE_GROUP" >/dev/null
}

passdb() { pdbedit -s "$SMB_CONF" -L -w 2>/dev/null | awk -F: '$1 ~ /^wd[0-9]+$/ { print $1 " " $5 }'; }
flags() { awk -v a="$1" '$1 == a { print $2 }' "$WORK/passdb"; }
sessions() { smbstatus -s "$SMB_CONF" -b 2>/dev/null | awk '$1 ~ /^[0-9]+$/ { print $1 " " $2 }' > "$WORK/sessions"; }
connected() { awk -v a="$1" '$2 == a { found = 1 } END { exit !found }' "$WORK/sessions"; }
kick() {
    # Each client connection has its own smbd process; ending it disconnects
    # only that device. Windows reconnects with the account's current state.
    awk -v a="$1" '$2 == a { print $1 }' "$WORK/sessions" | while read -r pid; do
        kill "$pid" 2>/dev/null
    done
}

observe() {
    passdb > "$WORK/passdb"
    sessions
    temporary=$(mktemp "$OBSERVED/.observed.XXXXXX") || return 1
    {
        printf 'generation\t%s\n' "$APPLIED"
        while read -r account accountflags; do
            enabled=1
            case "$accountflags" in *D*) enabled=0 ;; esac
            live=0
            connected "$account" && live=1
            printf 'device\t%s\t1\t%s\t%s\n' "$account" "$enabled" "$live"
        done < "$WORK/passdb"
    } > "$temporary" && chmod 0644 "$temporary" && mv -f "$temporary" "$OBSERVED/observed.tsv"
}

apply() {
    desired="$CONTROL/accounts.tsv"
    ensure_group "$DRIVE_GROUP" "$DRIVE_GID" || return 1
    if [ ! -f "$desired" ] || [ -L "$desired" ]; then
        observe
        return 0
    fi
    generation=$(awk -F'\t' '$1 == "generation" && $2 ~ /^[0-9]+$/ { print $2; exit }' "$desired")
    if [ -z "$generation" ]; then
        log "desired state has no generation; leaving accounts unchanged"
        observe
        return 1
    fi
    failed=0
    awk -F'\t' 'NF == 3 && $1 == "person" && $2 ~ /^[0-9]+$/ && $2 > 700000 && $2 < 800000 && $3 ~ /^wp[0-9]+$/ {
        print $2 " " $3 }' "$desired" > "$WORK/people"
    awk -F'\t' 'NF == 5 && $1 == "device" && $2 ~ /^[0-9]+$/ && $2 > 600000 && $2 < 699999 && $3 ~ /^wd[0-9]+$/ &&
        $4 ~ /^[0-9]+$/ && $4 > 700000 && $4 < 800000 && ($5 == "active" || $5 == "disabled") {
        print $2 " " $3 " " $4 " " $5 }' "$desired" > "$WORK/devices"
    while read -r gid name; do
        ensure_group "$name" "$gid" || failed=1
    done < "$WORK/people"
    passdb > "$WORK/passdb"
    sessions
    wanted=" "
    active=" "
    while read -r uid account gid state; do
        wanted="$wanted$account "
        [ "$state" = active ] && active="$active$account "
        ensure_user "$account" "$uid" "$gid" || { failed=1; continue; }
        secret="$CONTROL/secrets/$account"
        if [ -f "$secret" ] && [ ! -L "$secret" ]; then
            password=$(head -n 1 "$secret")
            if printf '%s' "$password" | grep -Eq '^[A-Za-z0-9_-]{20,128}$'; then
                if printf '%s\n%s\n' "$password" "$password" | smbpasswd -c "$SMB_CONF" -s -a "$account" >/dev/null; then
                    rm -f "$secret"
                else
                    failed=1
                fi
            else
                log "discarding a malformed one-time password for $account"
                rm -f "$secret"
            fi
            password=
            passdb > "$WORK/passdb"
        fi
        current=$(flags "$account")
        [ -n "$current" ] || continue
        if [ "$state" = active ]; then
            case "$current" in *D*) smbpasswd -c "$SMB_CONF" -e "$account" >/dev/null || failed=1 ;; esac
        else
            case "$current" in *D*) ;; *) smbpasswd -c "$SMB_CONF" -d "$account" >/dev/null || failed=1 ;; esac
            kick "$account"
        fi
    done < "$WORK/devices"
    # Remove every device account the application no longer lists. Deleting
    # before disconnecting prevents a reconnect between the two steps.
    { awk '{ print $1 }' "$WORK/passdb"; awk -F: '$1 ~ /^wd[0-9]+$/ { print $1 }' "$PASSWD_FILE"; } | sort -u |
    while read -r account; do
        case "$wanted" in *" $account "*) continue ;; esac
        if [ -n "$(flags "$account")" ]; then
            pdbedit -s "$SMB_CONF" -x -u "$account" >/dev/null 2>&1 || { log "could not remove $account"; continue; }
        fi
        kick "$account"
        [ -z "$(user_ids "$account")" ] || deluser "$account" >/dev/null 2>&1
    done
    # A device may have connected after the first snapshot: end every session
    # that does not belong to an active, listed device.
    sessions
    awk '$2 ~ /^wd[0-9]+$/ { print $2 }' "$WORK/sessions" | sort -u | while read -r account; do
        case "$active" in *" $account "*) ;; *) kick "$account" ;; esac
    done
    awk -F: '$1 ~ /^wp[0-9]+$/ { print $1 }' "$GROUP_FILE" | while read -r name; do
        awk -v n="$name" '$2 == n { found = 1 } END { exit !found }' "$WORK/people" || delgroup "$name" >/dev/null 2>&1
    done
    [ "$failed" = 0 ] && APPLIED=$generation
    observe
    return "$failed"
}

case "${1:-loop}" in
    once) apply ;;
    loop)
        while :; do
            apply || log "some accounts could not be applied; retrying"
            sleep "$INTERVAL"
        done
        ;;
    *) echo "usage: drive-agent.sh [once|loop]" >&2; exit 2 ;;
esac
