# Security Policy

## Supported Versions

The package is at 0.4.0. Only the current release is supported; there is no
maintenance branch for earlier versions.

| Version | Supported          |
| ------- | ------------------ |
| 0.4.x   | :white_check_mark: |
| < 0.4.0 | :x:                |

## Reporting a Vulnerability

If you discover a security issue, please open a
[private security advisory](https://github.com/Fengrru/honestcode/security/advisories/new)
rather than a public issue. GitHub notifies the maintainer and keeps the report
private until a fix is ready.

Please include:

- A description of the vulnerability.
- Steps to reproduce or a proof-of-concept.
- The version of HonestCode you are using.
- Your Python version and operating system.

## Sandbox Security Note

`execute_code` is designed to catch accidental mistakes and infinite loops, not
to contain malicious code. It runs in a subprocess with a temporary working
directory and best-effort resource limits (POSIX rlimits; a Windows Job Object),
but it does not provide full isolation: the child runs as the current user with
file-system and network access. Do not use it to execute untrusted code without
additional sandboxing such as a container or dedicated virtual machine.

## MCP Transport

The stdio transport inherits the permissions of the client that launched it. The
SSE transport has no authentication. Binding it to a non-loopback address now
requires `HONESTCODE_ALLOW_NETWORK=1`, and it should only be exposed behind a
separate access-control layer — a remote caller with `execute_code` enabled can
run code as the current user.

## Scope

This security policy applies to the source code in this repository.

## Disclosure Policy

We follow coordinated disclosure:

1. Report the vulnerability privately.
2. We acknowledge receipt within 48 hours.
3. We work with you to understand and validate the issue.
4. We develop and test a fix.
5. We release the fix and publicly disclose the vulnerability.

## Security Best Practices for Users

- Always run `execute_code` in a controlled environment.
- Keep your dependencies up to date (`pip install --upgrade`).
- Use virtual environments to isolate project dependencies.
- Review generated code before accepting it into your codebase.
