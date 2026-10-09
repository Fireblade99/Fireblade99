Put corporate root CA certificates here (PEM, `*.crt`) if the build network
re-signs HTTPS (pip then fails with CERTIFICATE_VERIFY_FAILED). They are added
to the image trust store. `*.crt` files are git-ignored.

Only CA certificates belong here: everything in this folder is copied into the
image. Server certificates and private keys for HTTPS go to ../tls/
(tls.crt, tls.key), which is mounted at runtime and never baked into the image.

