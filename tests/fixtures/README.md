# Local TLS test certificates

The `gateway` and `wrong` certificate/key pairs are self-signed synthetic test
fixtures, generated independently for this SDK with OpenSSL. Their DNS SANs are
`ep-00000000000000000000000000.example.test` and `wrong.example.test`,
respectively. Keys are public test data, never production secrets. To regenerate
either pair, replace NAME and HOST with the fixture name and corresponding SAN:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout tests/fixtures/NAME-key.pem -out tests/fixtures/NAME.pem \
  -subj '/CN=HOST' -addext 'subjectAltName=DNS:HOST' \
  -addext 'basicConstraints=critical,CA:FALSE' -addext 'extendedKeyUsage=serverAuth'
```

The prior fixtures were copied from Rust revision
`3b8d64fd692e3b2edaebf4233ec25bf15bcc53d6`; these replacements are not byte
copies. The historical authority fixture is unchanged and is not an input rule.
