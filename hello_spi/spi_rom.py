from nmigen import *
from math import ceil, log2
from nmigen.sim import *
from nmigen_soc.memory import *
from nmigen_soc.wishbone import *
from nmigen_boards.resources import *

# (Dummy SPI resources for simulated tests)
class DummyPin():
  def __init__( self, name ):
    self.o = Signal( name = '%s_o'%name )
    self.i = Signal( name = '%s_i'%name )
class DummySPI():
  def __init__( self ):
    self.cs   = DummyPin( 'cs' )
    self.clk  = DummyPin( 'clk' )
    self.mosi = DummyPin( 'mosi' )
    self.miso = DummyPin( 'miso' )

# Core SPI Flash "ROM" module.
class SPI_ROM( Elaboratable, Interface ):
  def __init__( self, dat_start, dat_end, data ):
    # Starting address in the Flash chip. This probably won't
    # be zero, because many FPGA boards use their external SPI
    # Flash to store the bitstream which configures the chip.
    self.dstart = dat_start
    # Last accessible address in the flash chip.
    self.dend = dat_end
    # Length of accessible data.
    self.dlen = ( dat_end - dat_start ) + 1
    # SPI Flash address command.
    self.spio = Signal( 32, reset = 0x03000000 )
    # Data counter.
    self.dc = Signal( 5, reset = 0b000000 )

    # Initialize Wishbone bus interface.
    Interface.__init__( self, addr_width = ceil( log2( self.dlen + 1 ) ), data_width = 32 )
    self.memory_map = MemoryMap( addr_width = self.addr_width, data_width = self.data_width, alignment = 0 )

    # Backing data store for a test ROM image. Not used when
    # the module is built for real hardware.
    if data is not None:
      self.data = Memory( width = 32, depth = len( data ), init = data )
    else:
      self.data = None

  def elaborate( self, platform ):
    m = Module()

    if platform is None:
      self.spi = DummySPI()
    else:
      self.spi = platform.request( 'spi_flash_1x' )

    # Clock rests at 0.
    m.d.comb += self.spi.clk.o.eq( 0 )
    # SPI Flash can only address 24 bits.
    m.d.comb += self.spio.eq( ( 0x03000000 | ( ( self.adr + self.dstart ) & 0x00FFFFFF ) ) )

    # Use a state machine for Flash access.
    # "Mode 0" SPI is very simple:
    # - Device is active when CS is low, inactive otherwise.
    # - Clock goes low, both sides write their bit if necessary.
    # - Clock goes high, both sides read their bit if necessary.
    # - Repeat ad nauseum.
    with m.FSM() as fsm:
      # 'Reset' and 'power-up' states:
      # pull CS low, then release power-down mode by sending 0xAB.
      # Normally this is not necessary, but iCE40 chips shut down
      # their connected SPI Flash after configuring themselves
      # in order to save power and prevent unintended writes.
      with m.State( "SPI_RESET" ):
        m.d.sync += self.spi.cs.o.eq( 1 )
        m.next = "SPI_POWERUP"
      with m.State( "SPI_POWERUP" ):
        m.d.sync += self.dc.eq( self.dc + 1 )
        m.d.comb += self.spi.clk.o.eq( ~ClockSignal( "sync" ) )
        m.d.comb += self.spi.mosi.o.eq( 0xAB >> ( 7 - self.dc ) )
        # Wait a few extra cycles after ending the transaction to
        # allow the chip to wake up from sleep mode.
        # TODO: Time this based on clock frequency?
        with m.If( self.dc == 30 ):
          m.d.sync += self.spi.cs.o.eq( 0 )
          m.next = "SPI_WAITING"
        # De-assert CS after sending 8 bits of data = 16 clock edges.
        with m.Elif( self.dc >= 8 ):
          m.d.sync += self.spi.cs.o.eq( 0 )
          m.next = "SPI_POWERUP"
        # Toggle the 'clk' pin every cycle.
        with m.Else():
          m.next = "SPI_POWERUP"
      # 'Waiting' state: Keep the 'cs' pin high until a new read is
      # requested, then move to 'SPI_TX' to send the read command.
      # Also keep 'ack' asserted until 'stb' is released.
      with m.State( "SPI_WAITING" ):
        m.d.sync += [
          self.ack.eq( self.cyc & ( self.ack & self.stb ) ),
          self.spi.cs.o.eq( 0 )
        ]
        m.next = "SPI_WAITING"
        with m.If( ( self.cyc == 1 ) & ( self.stb == 1 ) & ( self.ack == 0 ) ):
          m.d.sync += [
            self.spi.cs.o.eq( 1 ),
            self.ack.eq( 0 ),
            self.dc.eq( 31 )
          ]
          m.next = "SPI_TX"
      # 'Send read command' state: transmits the 0x03 'read' command
      # followed by the desired 24-bit address. (Encoded in 'spio')
      with m.State( "SPI_TX" ):
        # Set the 'mosi' pin to the next value and increment 'dc'.
        m.d.comb += self.spi.mosi.o.eq( self.spio >> self.dc )
        m.d.sync += self.dc.eq( self.dc - 1 )
        m.d.comb += self.spi.clk.o.eq( ~ClockSignal( "sync" ) )
        # Move to 'receive data' state once 32 bits have elapsed.
        # Also clear 'dat_r' and 'dc' before doing so.
        with m.If( self.dc == 0 ):
          m.d.sync += [
            self.dc.eq( 7 ),
            self.dat_r.eq( 0 )
          ]
          m.next = "SPI_RX"
        with m.Else():
          m.next = "SPI_TX"
      # 'Receive data' state: continue the clock signal and read
      # the 'miso' pin on rising edges.
      # You can keep the clock signal going to receive as many bytes
      # as you want, but this implementation only fetches one word.
      # Bytes are received in 'little-endian' format, MSbit-first.
      with m.State( "SPI_RX" ):
        # Simulate the 'miso' pin value for tests.
        if platform is None:
          m.d.comb += self.spi.miso.i.eq( ( self.data[ self.adr >> 2 ] >> self.dc ) & 0b1 )
        m.d.sync += [
          self.dc.eq( self.dc - 1 ),
          self.dat_r.eq( self.dat_r | ( self.spi.miso.i << self.dc ) )
        ]
        m.d.comb += self.spi.clk.o.eq( ~ClockSignal( "sync" ) )
        # Assert 'ack' signal and move back to 'waiting' state
        # once a whole word of data has been received.
        with m.If( self.dc[ :3 ] == 0 ):
          with m.If( self.dc[ 3 : 5 ] == 0b11 ):
            m.d.sync += [
              self.spi.cs.o.eq( 0 ),
              self.ack.eq( self.cyc )
            ]
            m.next = "SPI_WAITING"
          with m.Else():
            m.d.sync += self.dc.eq( self.dc + 15 )
            m.next = "SPI_RX"
        with m.Else():
          m.next = "SPI_RX"

    # (End of SPI Flash "ROM" module logic)
    return m

##############################
# SPI Flash "ROM" testbench: #
##############################
# Keep track of test pass / fail rates.
p = 0
f = 0

# Helper method to record unit test pass/fails.
def spi_rom_ut( name, actual, expected ):
  global p, f
  if expected != actual:
    f += 1
    print( "\033[31mFAIL:\033[0m %s (0x%08X != 0x%08X)"
           %( name, actual, expected ) )
  else:
    p += 1
    print( "\033[32mPASS:\033[0m %s (0x%08X == 0x%08X)"
           %( name, actual, expected ) )

# Helper method to test reading a byte of SPI data.
def spi_read_word( srom, virt_addr, phys_addr, simword, end_wait ):
  # Set 'address'.
  yield srom.adr.eq( virt_addr )
  # Set 'strobe' and 'cycle' to request a new read.
  yield srom.stb.eq( 1 )
  yield srom.cyc.eq( 1 )
  # Wait a tick; the (inverted) CS pin should then be low, and
  # the 'read command' value should be set correctly.
  yield Tick()
  yield Settle()
  csa = yield srom.spi.cs.o
  spcmd = yield srom.spio
  spi_rom_ut( "CS Low", csa, 1 )
  spi_rom_ut( "SPI Read Cmd Value", spcmd, ( phys_addr & 0x00FFFFFF ) | 0x03000000 )
  # Then the 32-bit read command is sent; two ticks per bit.
  for i in range( 32 ):
    yield Settle()
    dout = yield srom.spi.mosi.o
    spi_rom_ut( "SPI Read Cmd  [%d]"%i, dout, ( spcmd >> ( 31 - i ) ) & 0b1 )
    yield Tick()
  # The following 32 bits should return the word. Simulate
  # the requested word arriving on the MISO pin, MSbit first.
  # (Data starts getting returned on the falling clock edge
  #  immediately following the last rising-edge read.)
  i = 7
  expect = 0
  while i < 32:
    yield Tick()
    yield Settle()
    expect = expect | ( ( 1 << i ) & simword )
    progress = yield srom.dat_r
    spi_rom_ut( "SPI Read Word [%d]"%i, progress, expect )
    if ( ( i & 0b111 ) == 0 ):
      i = i + 15
    else:
      i = i - 1
  # Wait one more tick, then the CS signal should be de-asserted.
  yield Tick()
  yield Settle()
  csa = yield srom.spi.cs.o
  spi_rom_ut( "CS High (Waiting)", csa, 0 )
  # Done; reset 'strobe' and 'cycle' after N ticks to test
  # delayed reads from the bus.
  for i in range( end_wait ):
    yield Tick()
  yield srom.stb.eq( 0 )
  yield srom.cyc.eq( 0 )
  yield Tick()
  yield Settle()

# Top-level SPI ROM test method.
def spi_rom_tests( srom ):
  global p, f

  # Let signals settle after reset.
  yield Tick()
  yield Settle()

  # Print a test header.
  print( "--- SPI Flash 'ROM' Tests ---" )

  # Test basic behavior by reading a few consecutive words.
  yield from spi_read_word( srom, 0x00, 0x200000, 0x89ABCDEF, 0 )
  yield from spi_read_word( srom, 0x04, 0x200004, 0x0C0FFEE0, 4 )
  for i in range( 4 ):
    yield Tick()
    yield Settle()
    csa = yield srom.spi.cs.o
    spi_rom_ut( "CS High (Waiting)", csa, 0 )
  yield from spi_read_word( srom, 0x10, 0x200010, 0xDEADFACE, 1 )
  yield from spi_read_word( srom, 0x0C, 0x20000C, 0xABACADAB, 1 )

  # Done. Print the number of passed and failed unit tests.
  yield Tick()
  print( "SPI 'ROM' Tests: %d Passed, %d Failed"%( p, f ) )

# 'main' method to run a basic testbench.
if __name__ == "__main__":
  # Instantiate a test SPI ROM module.
  off = ( 2 * 1024 * 1024 )
  dut = SPI_ROM( off, off + 1024, [ 0x89ABCDEF, 0x0C0FFEE0, 0xBABABABA, 0xABACADAB, 0xDEADFACE, 0x12345678, 0x87654321, 0xDEADBEEF, 0xDEADBEEF ] )

  # Run the SPI ROM tests.
  sim = Simulator( dut )
  def proc():
    # Wait until the 'release power-down' command is sent.
    # TODO: test that startup condition.
    for i in range( 30 ):
      yield Tick()
    yield from spi_rom_tests( dut )
  sim.add_clock( 1e-6 )
  sim.add_sync_process( proc )
  with sim.write_vcd( "spi_rom.vcd" ):
    sim.run()
