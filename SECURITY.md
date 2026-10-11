# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1.0 | :x:                |

## Reporting a Vulnerability

If you discover a security issue, please email the maintainers directly rather
than opening a public issue. We will respond as quickly as possible and work
with you to assess and address the problem responsibly.

**Email:** security@honestcode.example (replace with a real address when
available).

Please include:

- A description of the vulnerability.
- Steps to reproduce or a proof-of-concept.
- The version of HonestCode you are using.
- Your Python version and operating system.

## Sandbox Security Note

`execute_code` is designed to catch accidental mistakes and infinite loops, not
to contain malicious code. It runs in a subprocess with a temporary working
directory and optional Unix resource limits, but it does not provide full
isolation. Do not use it to execute untrusted code without additional sandboxing
such as a container or dedicated virtual machine.

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
