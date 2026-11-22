# Troubleshooting log

These are the real faults met while building the lab, in the order they happened.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | GNS3: "Cannot connect to compute" | The VirtualBox host-only adapter had reverted to 192.168.56.1 while DHCP still served 192.168.103.x | Set the adapter back to 192.168.103.1/24. The GNS3 VM API is on port 80 |
| 2 | R3 ↔ R4 100% ping loss, ARP failed, even though the interfaces were UP and some IS-IS neighbours were Up | Addresses had been applied assuming the eth names followed the GNS3 port order. Four of the five links had different subnets on each end | Proved four ways: GNS3 labels, `hw-id` MACs, Wireshark, and the IPv6 link-locals in `show isis neighbor detail`. Addresses were moved on R1, R3 and R4, one router at a time, using `compare` + `commit-confirm` + ping |
| 3 | Wrong commands staged on R1 | R3's block was pasted into R1 | Caught at `compare` and removed with `discard` before commit |
| 4 | Ubuntu had two management addresses | An old static address plus a cloud-init DHCP lease | Cleaned up with netplan / cloud-init / nmcli, leaving only 192.168.103.10 |
| 5 | RESTCONF PUT returned **HTTP 415** | The header was typed as `ContentType` instead of `Content-Type` | Corrected the header; all PUTs then returned 201 |
| 6 | iperf3 UDP failed between loopbacks | The replies came back from a different source address | Tested interface to interface (10.103.0.1 → 10.103.0.2) |
| 7 | BGP stayed **Active** after a cold start | ODL was not listening yet | `reset bgp 192.168.103.10` once Karaf was up |
| 8 | TE bandwidth was not advertised | FRR needs `mpls-te on` under `router isis` | Added it in vtysh (runtime only) |
| 9 | Ubuntu Desktop VM hung at the boot logo on demo day | A graphical desktop on a host at about 94% RAM | Rebuilt the controller on Ubuntu Server with the same IP, so R1 needed no changes |
| 10 | apt and wget failed on campus | Plain HTTP (port 80) is blocked; HTTPS is allowed | Used `https://za.archive.ubuntu.com/ubuntu/` and downloaded ODL over HTTPS |

## Useful checks

```text
# VyOS
show interfaces
show isis neighbor [detail]
show ip route isis
show bgp summary
show ip bgp neighbor 192.168.103.10
vtysh -c "show isis database detail R1.00-00"

# Ubuntu / ODL
sudo ss -tlnp | grep -E '1790|8181'
sudo ss -tnp | grep 1790
curl -s -u admin:admin -H "Accept: application/xml" \
  "http://localhost:8181/rests/data/bgp-rib:bgp-rib/rib=bgp-to-r1/loc-rib?content=nonconfig"
```
