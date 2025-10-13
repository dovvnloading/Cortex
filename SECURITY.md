# Security Policy for Cortex

## Our Commitment to Security

The security and privacy of our users are the highest priorities for the Cortex project. Our core design philosophy is to be a **private, secure, local-first AI assistant**. This means your data, models, and conversations are intended to stay on your device and never be sent to the cloud or any third party.

We take all security reports seriously and are committed to addressing them in a timely and responsible manner.

## Supported Versions

We provide security updates for the most recent stable version of Cortex. Please ensure you are running the latest version before reporting a vulnerability.

| Version   | Supported          |
| :-------- | :----------------- |
| >= 0.94.x | :white_check_mark: |
| < 0.94.x  | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.** Publicly disclosing a vulnerability could put other users at risk before a solution is available.

We encourage you to report potential security issues privately. We will work with you to understand the scope of the vulnerability and will publicly credit you for your discovery if you wish.

### Preferred Method: Private Vulnerability Reporting

The best way to report a vulnerability is through GitHub's private vulnerability reporting feature. This ensures the report is delivered directly to the maintainers without being publicly visible.

1.  Go to the [**"Security" tab of the Cortex repository**](https://github.com/dovvnloading/Cortex/security).
2.  Click on **"Report a vulnerability"**.
3.  Fill out the form with as much detail as possible.

### Alternative Method: Email

If you cannot use GitHub's private reporting, you can send an email to:
**[your-security-contact-email@example.com]**

*(Note: Replace the placeholder email with a real contact address. If you don't have a dedicated one, your primary contact email is fine.)*

Please use the subject line: `[SECURITY] Vulnerability in Cortex`

### What to Include in Your Report

To help us resolve the issue quickly, please provide a detailed report including:

-   **A clear description** of the vulnerability and its potential impact.
-   **Step-by-step instructions** to reproduce the issue.
-   **The version of Cortex** you are using.
-   **Your environment** (e.g., Operating System, Ollama version).
-   **Any proof-of-concept code, screenshots, or logs** that can help us understand the issue.

## Our Process

When you report a vulnerability, we commit to the following:

1.  We will acknowledge receipt of your report within **48-72 hours**.
2.  We will investigate the issue and confirm its validity.
3.  We will work on a fix and keep you updated on our progress.
4.  Once a patch is released, we will notify you and, if you agree, we will publicly credit you in the release notes.

## Scope

### In Scope

We are interested in any vulnerability that could compromise the security or privacy of our users, including but not limited to:

-   Any breach of our "local-first" promise (e.g., unintended network requests to third parties).
-   Remote Code Execution (RCE).
-   Unauthorized access to local chat history or the permanent memory database.
-   Vulnerabilities in the application's dependencies that directly affect Cortex's security.

### Out of Scope

The following issues are generally considered out of scope for our security policy:

-   **Vulnerabilities in Ollama itself.** These should be reported directly to the [Ollama project](https://github.com/ollama/ollama).
-   **Model-specific issues.** This includes prompt injection, "jailbreaking," or getting the LLM to produce harmful or biased content. These are inherent challenges of LLMs and should be addressed at the model level.
-   **Security of the user's underlying operating system.** Cortex relies on the security of the environment it runs in.
-   **Social engineering attacks** against users of Cortex.
-   **Issues related to binaries downloaded from unofficial sources.** We can only guarantee the integrity of binaries provided in our official download links.

Thank you for helping us keep Cortex safe and secure for everyone.
