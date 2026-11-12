# Debian 13 bootstrap archive keys

`archive.gpg` contains only public keys downloaded from Debian's official archive key site on 2026-11-12. It is used to verify debootstrap's Debian 13 archive on older Ubuntu build hosts whose packaged Debian keyring may not yet include trixie. Guest APT uses the `debian-archive-keyring` package installed from that verified archive.

Source and published fingerprints: https://ftp-master.debian.org/keys.html

| Public key URL | Primary fingerprint |
|---|---|
| https://ftp-master.debian.org/keys/archive-key-13.asc | `04B54C3CDCA79751B16BC6B5225629DF75B188BD` |
| https://ftp-master.debian.org/keys/archive-key-13-security.asc | `5E04A1E3223A19A20706E20F9904613D4CCE68C6` |
| https://ftp-master.debian.org/keys/release-13.asc | `41587F7DB8C774BCCF131416762F67A0B2C39DE4` |

The fingerprints were checked using `gpg --show-keys --with-colons` against the official page. Keys are binary OpenPGP public-key packets, not secret keys. Updating them requires reviewing official Debian key announcements; do not disable signature verification to work around expiry or key errors.
