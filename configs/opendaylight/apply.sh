#!/usr/bin/env bash
# Push the BGP configuration to OpenDaylight 0.20.3 via RESTCONF (RFC 8040).
# Run on the ODL server after: ./bin/karaf  ->  feature:install odl-restconf odl-bgpcep-bgp
set -e
B="http://localhost:8181/rests/data"
P="$B/openconfig-network-instance:network-instances/network-instance=global-bgp/openconfig-network-instance:protocols/protocol=openconfig-policy-types:BGP,bgp-to-r1"
H='Content-Type: application/xml'
curl -s -u admin:admin -X PUT -H "$H" -d @bgp-instance.xml  -w "\ninstance HTTP %{http_code}\n" "$P"
curl -s -u admin:admin -X PUT -H "$H" -d @peer-acceptor.xml -w "\nacceptor HTTP %{http_code}\n" "$B/odl-bgp-peer-acceptor-config:bgp-peer-acceptor-config=default"
curl -s -u admin:admin -X PUT -H "$H" -d @r1-neighbor.xml   -w "\nneighbor HTTP %{http_code}\n" "$P/bgp-openconfig-extensions:bgp/neighbors/neighbor=192.168.103.11"

# Checks
sudo ss -tlnp | grep -E '1790|8181'
curl -s -u admin:admin -H "Accept: application/xml" "$B/bgp-rib:bgp-rib/rib=bgp-to-r1/loc-rib?content=nonconfig"; echo
