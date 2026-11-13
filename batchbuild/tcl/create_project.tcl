# Create a test project: one block design with a handful of AXI peripherals,
# each synthesised out of context as its own run.
#
#   vivado -mode batch -source create_project.tcl -tclargs <proj_dir> [part]

set proj_dir [lindex $argv 0]
set part     [expr {[llength $argv] > 1 ? [lindex $argv 1] : "xcvc1902-vsva2197-2MP-e-S"}]

create_project bbtest $proj_dir -part $part
set_property target_language Verilog [current_project]
# The comparison is about run overhead, so every run must really synthesise.
config_ip_cache -disable_cache

proc ip {name} {
    set defs [lsort [get_ipdefs -all "xilinx.com:ip:${name}:*"]]
    return [lindex $defs end]
}

create_bd_design top

set clk  [create_bd_port -dir I -type clk -freq_hz 100000000 clk]
set rstn [create_bd_port -dir I -type rst rstn]
set s_axi [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI]
set_property -dict [list CONFIG.PROTOCOL AXI4LITE CONFIG.DATA_WIDTH 32 \
    CONFIG.ADDR_WIDTH 32 CONFIG.FREQ_HZ 100000000] $s_axi
set_property CONFIG.ASSOCIATED_BUSIF S_AXI $clk

create_bd_cell -type ip -vlnv [ip proc_sys_reset] rst
connect_bd_net $clk  [get_bd_pins rst/slowest_sync_clk]
connect_bd_net $rstn [get_bd_pins rst/ext_reset_in]

set peripherals {gpio0 axi_gpio gpio1 axi_gpio uart axi_uartlite timer axi_timer
                 iic axi_iic bram_ctrl axi_bram_ctrl}
set n [expr {[llength $peripherals] / 2}]

create_bd_cell -type ip -vlnv [ip smartconnect] xbar
set_property -dict [list CONFIG.NUM_SI 1 CONFIG.NUM_MI $n] [get_bd_cells xbar]
connect_bd_intf_net $s_axi [get_bd_intf_pins xbar/S00_AXI]
connect_bd_net $clk [get_bd_pins xbar/aclk]
connect_bd_net [get_bd_pins rst/interconnect_aresetn] [get_bd_pins xbar/aresetn]

set i 0
foreach {name type} $peripherals {
    create_bd_cell -type ip -vlnv [ip $type] $name
    connect_bd_intf_net [get_bd_intf_pins xbar/M0${i}_AXI] [get_bd_intf_pins $name/S_AXI]
    connect_bd_net $clk [get_bd_pins $name/s_axi_aclk]
    connect_bd_net [get_bd_pins rst/peripheral_aresetn] [get_bd_pins $name/s_axi_aresetn]
    incr i
}
set_property CONFIG.SINGLE_PORT_BRAM 1 [get_bd_cells bram_ctrl]
create_bd_cell -type ip -vlnv [ip emb_mem_gen] bram
connect_bd_intf_net [get_bd_intf_pins bram_ctrl/BRAM_PORTA] [get_bd_intf_pins bram/BRAM_PORTA]

# Leave the peripherals' I/O as external ports so nothing is optimised away.
foreach intf {gpio0/GPIO gpio1/GPIO uart/UART iic/IIC} {
    make_bd_intf_pins_external [get_bd_intf_pins $intf]
}
foreach pin {timer/interrupt uart/interrupt iic/iic2intc_irpt} {
    make_bd_pins_external [get_bd_pins $pin]
}

assign_bd_address
validate_bd_design
save_bd_design

set bd_file [get_files top.bd]
set_property synth_checkpoint_mode Hierarchical $bd_file
generate_target all $bd_file
make_wrapper -files $bd_file -top
add_files -norecurse [glob $proj_dir/bbtest.gen/sources_1/bd/top/hdl/top_wrapper.v]
set_property top top_wrapper [current_fileset]

# Create the OOC runs without launching them.
create_ip_run $bd_file
puts "OOC runs: [get_runs -filter {IS_SYNTHESIS && NAME != synth_1}]"
close_project
