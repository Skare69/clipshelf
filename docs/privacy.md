# Accounts and privacy

- Invitation-only: an admin issues a single-use link; the recipient picks the
  password and verifies the mailbox.
- Personal collections are private and non-transferable. Shared collections have
  owners and members; members remove only their own contributions. Membership is
  granted by owner invitation: the owner sends an invite by email and the invitee
  joins through the emailed link. The member endpoint answers every address with
  the same response — it never reveals whether a mailbox has an account.
- App admins manage accounts, invitations, shared-collection succession and the
  model connection. They can restore access to an account that lost both its
  password and mailbox — deliberately, Jellyfin style — but that is identity
  recovery, not permission to read other people's collections.
- Sensitive admin actions need recent reauthentication. Password retries use
  allauth's account reauthentication and failed-login limits.
- Accounts are disabled, never deleted; disabling revokes sessions and keeps
  content, memberships and ownership.
- Tailscale is transport only. LAN and remote requests get identical checks, and
  forwarded/identity headers never authenticate anyone.
- The server makes no third-party request by default. The optional release
  check, enabled with `CLIPSHELF_UPDATE_CHECK=1`, asks api.github.com for the
  latest release. A good answer stops new requests for one hour; a failed
  check retries after 60 seconds, so continued failures can ask several times
  in one hour. Each request shows the server IP and the time of the check to
  GitHub.
