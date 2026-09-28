{
  description = "Sandman v5 prototype";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      # nix develop            → shell with python + all dependencies
      # nix develop -c python -m sandman chat
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (p: [ p.requests p.jsonschema p.dateparser p.pytest ]))
          ];
          shellHook = ''
            export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
            export NIX_SSL_CERT_FILE=$SSL_CERT_FILE
            export REQUESTS_CA_BUNDLE=$SSL_CERT_FILE
          '';
        };
      });
    };
}
