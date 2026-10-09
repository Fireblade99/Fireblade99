Put corporate root CA certificates here (PEM, `*.crt`) if the build network
re-signs HTTPS (pip then fails with CERTIFICATE_VERIFY_FAILED). They are added
to the image trust store. `*.crt` files are git-ignored.
