#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This file is part of **python-openzwave** project https://github.com/OpenZWave/python-openzwave.
    :platform: Unix, Windows, MacOS X

.. moduleauthor:: bibi21000 aka Sébastien GALLET <bibi21000@gmail.com>

License : GPL(v3)

**python-openzwave** is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

**python-openzwave** is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
You should have received a copy of the GNU General Public License
along with python-openzwave. If not, see http://www.gnu.org/licenses.

"""
import logging
h = logging.NullHandler()
logger = logging.getLogger(__name__).addHandler(h)
import time
import argparse
import datetime
import threading
import queue

RESET_DOC = """
Tips for my UltraPro Z-Wave toggle switch:

Factory reset (run --hard_reset *after* this step):
    Quickly press up up up down down down.

Join the network:
    Press up.
"""

def list_nodes(args):
    if args.timeout is None:
        args.timeout = 60*60*4+1
    import openzwave
    from openzwave.node import ZWaveNode
    from openzwave.value import ZWaveValue
    from openzwave.scene import ZWaveScene
    from openzwave.controller import ZWaveController
    from openzwave.network import ZWaveNetwork
    from openzwave.option import ZWaveOption

    #Define some manager options
    print("-------------------------------------------------------------------------------")
    print("Define options for device {0}".format(args.device))
    options = ZWaveOption(device=args.device, config_path=args.config_path, user_path=args.user_path)
    options.set_log_file("OZW_Log.log")
    options.set_append_log_file(False)
    options.set_console_output(False)
    options.set_save_log_level("Debug")
    options.set_logging(True)
    options.lock()

    print("Start network")
    network = ZWaveNetwork(options, log=None)

    delta = 0.5
    for i in range(0, int(args.timeout/delta)):
        time.sleep(delta)
        if network.state >= network.STATE_AWAKED:
            break

    print("-------------------------------------------------------------------------------")
    print("Network is awaked. Talk to controller.")
    print("Get python_openzwave version : {}".format(network.controller.python_library_version))
    print("Get python_openzwave config version : {}".format(network.controller.python_library_config_version))
    print("Get python_openzwave flavor : {}".format(network.controller.python_library_flavor))
    print("Get openzwave version : {}".format(network.controller.ozw_library_version))
    print("Get config path : {}".format(network.controller.library_config_path))
    print("Controller capabilities : {}".format(network.controller.capabilities))
    print("Controller node capabilities : {}".format(network.controller.node.capabilities))
    print("Nodes in network : {}".format(network.nodes_count))

    print("-------------------------------------------------------------------------------")
    if args.timeout > 1800:
        print("You defined a really long timneout. Please use --help to change this feature.")
    print("Wait for network ready ({0}s)".format(args.timeout))
    for i in range(0, int(args.timeout/delta)):
        time.sleep(delta)
        if network.state == network.STATE_READY:
            break
    print("-------------------------------------------------------------------------------")
    if network.state == network.STATE_READY:
        print("Network is ready. Get nodes")
    elif network.state == network.STATE_AWAKED:
        print("Network is awake. Some sleeping devices may miss. You can increase timeout to get them. But will continue.")
    else:
        print("Network is still starting. You MUST increase timeout. But will continue.")

    for node in network.nodes:

        print("------------------------------------------------------------")
        print("{} - Name : {} ( Location : {} )".format(network.nodes[node].node_id, network.nodes[node].name, network.nodes[node].location))
        print(" {} - Ready : {} / Awake : {} / Failed : {}".format(network.nodes[node].node_id, network.nodes[node].is_ready, network.nodes[node].is_awake, network.nodes[node].is_failed))
        print(" {} - Manufacturer : {}  ( id : {} )".format(network.nodes[node].node_id, network.nodes[node].manufacturer_name, network.nodes[node].manufacturer_id))
        print(" {} - Product : {} ( id  : {} / type : {} / Version : {})".format(network.nodes[node].node_id, network.nodes[node].product_name, network.nodes[node].product_id, network.nodes[node].product_type, network.nodes[node].version))
        print(" {} - Command classes : {}".format(network.nodes[node].node_id, network.nodes[node].command_classes_as_string))
        print(" {} - Capabilities : {}".format(network.nodes[node].node_id, network.nodes[node].capabilities))
        print(" {} - Neighbors : {} / Power level : {}".format(network.nodes[node].node_id, network.nodes[node].neighbors, network.nodes[node].get_power_level()))
        print(" {} - Is sleeping : {} / Can wake-up : {} / Battery level : {}".format(network.nodes[node].node_id, network.nodes[node].is_sleeping, network.nodes[node].can_wake_up(), network.nodes[node].get_battery_level()))

    print("------------------------------------------------------------")
    print("Driver statistics : {}".format(network.controller.stats))
    print("------------------------------------------------------------")
    print("Stop network")
    network.stop()
    print("Exit")


class Switch:
    def __init__(self, switch_id):
        self.switch_id = switch_id
        self.onoff = None

    def __str__(self):
        return "Switch %r, onoff=%r" % (self.switch_id, self.onoff)


class StateTracker:
    def __init__(self, timeout):
        self._timeout = timeout
        self._q = queue.Queue()
        self.switches = {}
        self.home_id = None

    def watcher_cb(self, zwargs):
        print("%s %s" % (datetime.datetime.now().strftime("%H:%M:%S.%f"), zwargs))
        self._q.put(zwargs)

    def wait_for_nodes(self):
        if self.home_id is not None:
            raise AssertionError("Can't wait_for_nodes() with existing home_id")
        self.home_id = self.match("DriverReady")['homeId']
        self.match("AllNodesQueried")

    def wait_for_driver_removed(self):
        self.match("DriverRemoved")
        self.home_id = None
        self.switches.clear()

    def _q_get(self, timeout):
        zwargs = self._q.get(block=True, timeout=timeout)
        ntype = zwargs["notificationType"]
        if ntype == "ValueAdded" and zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY":
            switch_id = zwargs["valueId"]["id"]
            switch = Switch(switch_id)
            print("Adding %s" % switch)
            self.switches[switch_id] = switch
        elif ntype == "ValueChanged" and zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY":
            switch_id = zwargs["valueId"]["id"]
            onoff = zwargs["valueId"]["value"]
            try:
                switch = self.switches[switch_id]
            except KeyError:
                print("Unknown switch %r" % switch_id)
            else:
                switch.onoff = onoff
                print(switch)
                # XXX do something here
        return zwargs

    def q_passive(self, timeout):
        try:
            self._q_get(timeout)
        except queue.Empty:
            pass

    def match(self, notify_type, *match):
        if match:
            note = " with %s=%r" % ("".join("[%r]" % m for m in match[:-1]), match[-1])
        else:
            note = ""
        print("=== Waiting for %r%s ===" % (notify_type, note))
        timeout = self._timeout
        while True:
            start = time.time()
            zwargs = self._q_get(timeout=timeout)
            timeout -= (time.time() - start)
            if zwargs["notificationType"] != notify_type:
                continue
            if match:
                z = zwargs
                for m in match[:-1]:
                    z = z[m]
                if z != match[-1]:
                    continue
            return zwargs

def hard_reset(args):
    print("hard_reset")
    if args.timeout is None:
        args.timeout = 60
    import libopenzwave
    import openzwave
    from openzwave.option import ZWaveOption

    #Define some manager options
    print("-------------------------------------------------------------------------------")
    print("Define options for device {0}".format(args.device))
    options = ZWaveOption(device=args.device, config_path=args.config_path, user_path=args.user_path)
    options.set_log_file("OZW_Log.log")
    options.set_append_log_file(False)
    options.set_console_output(False)
    options.set_save_log_level("Debug")
    options.set_logging(True)
    options.lock()

    st = StateTracker(args.timeout)

    manager = libopenzwave.PyManager()
    manager.create()
    manager.addWatcher(st.watcher_cb)
    manager.addDriver(args.device)

    st.wait_for_nodes()

    print("Resetting controller...")
    manager.resetController(st.home_id)
    st.wait_for_driver_removed()
    st.wait_for_nodes()

    # XXX probably want to add N switches here?
    print("Adding node...")
    manager.addNode(st.home_id, doSecurity=False)
    zwargs = st.match("ControllerCommand", "controllerState", "Waiting")
    print(RESET_DOC)

    st.match("ValueAdded", "valueId", "commandClass", "COMMAND_CLASS_SWITCH_BINARY")
    st.match("ControllerCommand", "controllerState", "Completed")

    print("Everything seems fine!")
    manager.destroy()


def co2(args):
    if args.timeout is None:
        args.timeout = 60
    import libopenzwave
    import openzwave
    from openzwave.option import ZWaveOption

    #Define some manager options
    print("-------------------------------------------------------------------------------")
    print("Define options for device {0}".format(args.device))
    options = ZWaveOption(device=args.device, config_path=args.config_path, user_path=args.user_path)
    options.set_log_file("OZW_Log.log")
    options.set_append_log_file(False)
    options.set_console_output(False)
    options.set_save_log_level("Debug")
    options.set_logging(True)
    options.lock()

    st = StateTracker(args.timeout)

    manager = libopenzwave.PyManager()
    manager.create()
    manager.addWatcher(st.watcher_cb)
    manager.addDriver(args.device)

    st.wait_for_nodes()
    print("Active switch count: %d" % len(st.switches))

    # Turn off spam for now.
    #manager.setPollInterval(250, True)  # 4 Hz
    #manager.enablePoll(switch_value_id)

    def wait_for_quiet():
        old_q = None
        while True:
            q = manager.getSendQueueCount(st.home_id)
            if q == 0 and old_q is None:
                return
            if q != old_q:
                print("Waiting for SendQueue: %d" % q)
            if q == 0:
                return
            old_q = q
            time.sleep(0.1)

    # HACKY
    # pip3 install adafruit-circuitpython-scd30
    import board
    import adafruit_scd30

    i2c = board.I2C()   # uses board.SCL and board.SDA
    scd = adafruit_scd30.SCD30(i2c)

    onoff = 0  # 0 off, 1 on.

    while True:
        while not scd.data_available:
            time.sleep(0.5)

        co2 = scd.CO2
        if co2 > 800:
            wait_for_quiet()
            for switch_id in st.switches:
                manager.setValue(switch_id, 1)
            onoff = 1
        elif co2 < 750:
            wait_for_quiet()
            for switch_id in st.switches:
                manager.setValue(switch_id, 0)
            onoff = 0

        ts = datetime.datetime.now().replace(microsecond=0).isoformat(" ")
        print("%s, %d, %d" % (ts, co2, onoff), flush=True)

        # Passively consume messages for a while.
        start = time.time()
        while time.time() < start + 60:
            st.q_passive(1.0)

def pyozw_parser():
    parser = argparse.ArgumentParser(description='Run python_openzwave basics checks.')
    parser.add_argument('-d', '--device', action='store', help='The device port', default=None)
    parser.add_argument('-l', '--list_nodes', action='store_true', help='List the nodes on zwave network', default=False)
    parser.add_argument('-t', '--timeout', action='store',type=int, help='The default timeout for zwave network sniffing', default=None)
    parser.add_argument('--config_path', action='store', help='The config_path for openzwave', default=None)
    parser.add_argument('--user_path', action='store', help='The user_path for openzwave', default=".")


    # Hacks
    parser.add_argument('--hard_reset', action='store_true', help='XXX', default=False)
    parser.add_argument('--co2', action='store_true', help='XXX', default=False)
    return parser

def main():
    parser = pyozw_parser()
    args = parser.parse_args()
    if args.list_nodes:
        list_nodes(args)
    elif args.hard_reset:
        hard_reset(args)
    elif args.co2:
        co2(args)

if __name__ == '__main__':
    main()

