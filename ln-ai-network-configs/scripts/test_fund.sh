# Generate spendable coins
bitcoin-cli -regtest -datadir=runtime/bitcoind generatetoaddress 101 \
  $(bitcoin-cli -regtest -datadir=runtime/bitcoind getnewaddress)

# Get Lightning node address
lightning-cli --lightning-dir=runtime/nodes/node1 newaddr

# Fund node wallet
bitcoin-cli -regtest -datadir=runtime/bitcoind sendtoaddress <ADDR> 1

# Confirm funds
lightning-cli --lightning-dir=runtime/nodes/node1 listfunds
