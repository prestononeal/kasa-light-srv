#!/bin/zsh
set -eu

cd -- "${0:A:h}"
service='kasa-light-srv'
keychain="$HOME/Library/Keychains/login.keychain-db"

if ! item=$(/usr/bin/security find-generic-password -s "$service" "$keychain"); then
    print -u2 'Add a kasa-light-srv password item to your login Keychain.'
    exit 1
fi

KASA_USERNAME=$(print -r -- "$item" | /usr/bin/sed -n 's/^[[:space:]]*"acct"<blob>="\([^"]*\)"$/\1/p')
if [[ -z "$KASA_USERNAME" ]]; then
    print -u2 'Set the account name on the kasa-light-srv Keychain item to your TP-Link email.'
    exit 1
fi

if ! KASA_PASSWORD=$(/usr/bin/security find-generic-password \
    -s "$service" -a "$KASA_USERNAME" -w "$keychain"); then
    print -u2 'The launcher could not read the password. Unlock your login Keychain and allow access when prompted.'
    exit 1
fi

export KASA_USERNAME KASA_PASSWORD
exec .venv/bin/python -u server.py "$@"
