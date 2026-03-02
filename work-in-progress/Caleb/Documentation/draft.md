Rough Draft for Documentation


Caleb (MacOS, Silicon M2): 

Caleb has to install the Bitcoin Core backend to be able to run LND, Rust Lightning, and Eclair. 

To install Bitcoin Core:

brew install bitcoin
mkdir -p ~/Library/Application\ Support/Bitcoin

Create ~/Library/Application Support/Bitcoin/bitcoin.conf:
server=1
txindex=1
fallbackfee=0.0002

[regtest]
rpcbind=127.0.0.1
rpcbind=::1
rpcallowip=127.0.0.1

rpcuser=bitcoinrpc
rpcpassword=change_me

zmqpubrawblock=tcp://127.0.0.1:28332
zmqpubrawtx=tcp://127.0.0.1:28333

Run Bitcoin Core(1):
bitcoind -regtest -datadir="$HOME/Library/Application Support/Bitcoin"

In a new terminal: 

pgrep -fl bitcoind

lsof -nP -iTCP:18443 | grep LISTEN

bitcoin-cli -regtest createwallet "miner"
ADDR=$(bitcoin-cli -regtest getnewaddress)
bitcoin-cli -regtest generatetoaddress 101 "$ADDR"
bitcoin-cli -regtest getbalance

If balance is non-zero, then bitcoin core works


Processes to load the bitcoin wallet after creation(2):

bitcoin-cli -regtest listwalletdir
bitcoin-cli -regtest loadwallet "miner"
bitcoin-cli -regtest listwallets
ADDR=$(bitcoin-cli -regtest getnewaddress)
bitcoin-cli -regtest generatetoaddress 101 "$ADDR"
bitcoin-cli -regtest getbalance

Checking if ZMQ ports are listening(3):

lsof -nP -iTCP:28332 | grep LISTEN
lsof -nP -iTCP:28333 | grep LISTEN




Starting LND:

brew install go git git 
clone https://github.com/lightningnetwork/lnd.git 
cd lnd 
make install

Check if everything is there:

which lnd lncli
lnd --version
lncli --version

Start LND on regtest(1):

mkdir -p ~/.lnd-regtest

lnd \
  --lnddir=$HOME/.lnd-regtest \
  --listen=127.0.0.1:9735 \
  --rpclisten=127.0.0.1:10009 \
  --restlisten=127.0.0.1:8080 \
  --bitcoin.active \
  --bitcoin.regtest \
  --bitcoin.node=bitcoind \
  --bitcoind.rpchost=127.0.0.1 \
  --bitcoind.rpcuser=bitcoinrpc \
  --bitcoind.rpcpass=change_me \
  --bitcoind.zmqpubrawblock=tcp://127.0.0.1:28332 \
  --bitcoind.zmqpubrawtx=tcp://127.0.0.1:28333


In a new terminal, creating the wallet:

lncli --lnddir=$HOME/.lnd-regtest create



Cipher Seed:

---------------BEGIN LND CIPHER SEED---------------
 1. ability   2. sketch     3. trend      4. park   
 5. coffee    6. work       7. equal      8. neglect
 9. cool     10. fit       11. fiction   12. day    
13. design   14. bind      15. virtual   16. cabin  
17. river    18. festival  19. medal     20. base   
21. radar    22. arctic    23. solution  24. vacuum 
---------------END LND CIPHER SEED-----------------


How to start after LND is installed(2):

lncli --lnddir=$HOME/.lnd-regtest unlock

Input the password: fescuw-dupcu9-quHsyv



Testing to see if LND is working properly(3):

lncli --lnddir=$HOME/.lnd-regtest --network=regtest getinfo
lncli --lnddir=$HOME/.lnd-regtest --network=regtest walletbalance


How to start/install Rust Lightning:


brew install rustup-init


Adding rustup to PATH:

echo 'export PATH="/opt/homebrew/opt/rustup/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
hash -r

Setting default toolchain:

rustup default stable

Verify:

rustup --version
rustc --version
cargo --version

Installing LDK:

git clone https://github.com/lightningdevkit/ldk-sample
cd ldk-sample
cargo build --release

Making a storage dir for LDK:
mkdir -p ~/.ldk-regtest

Start LDK:
./target/release/ldk-sample \
  bitcoinrpc:change_me@127.0.0.1:18443 \
  ~/.ldk-regtest \
  9737 \
  regtest

Eclair:

Installing Java 21:
brew install openjdk@21

Putting Java 21 in PATH:
echo 'export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
hash -r
java -version

Downloading Eclair:






