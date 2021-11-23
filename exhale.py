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
import argparse
import asyncio
import collections
import datetime
import threading
import time
import traceback

# pip3 install adafruit-circuitpython-scd30
import board
import adafruit_scd30

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
    def __init__(self, node_id, switch_id):
        self.node_id = node_id
        self.switch_id = switch_id
        self.onoff = None

    def __str__(self):
        return "Switch node_id=%r, switch_id=%r, onoff=%r" % (self.node_id, self.switch_id, self.onoff)


class StateTracker:
    def __init__(self, timeout):
        self._timeout = timeout
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self.switches = {}
        self.home_id = None

    def threadsafe_watcher_cb(self, zwargs):
        print("%s %s" % (datetime.datetime.now().strftime("%H:%M:%S.%f"), zwargs))
        self._loop.call_soon_threadsafe(lambda: self._q.put_nowait(zwargs))

    async def wait_for_nodes(self):
        if self.home_id is not None:
            raise AssertionError("Can't wait_for_nodes() with existing home_id")
        zwargs = await self.match("DriverReady")
        self.home_id = zwargs['homeId']
        await self.match_any(["AllNodesQueried", "AllNodesQueriedSomeDead"])

    async def wait_for_driver_removed(self):
        await self.match("DriverRemoved")
        self.home_id = None
        self.switches.clear()

    async def _q_get(self, timeout):
        zwargs = await asyncio.wait_for(self._q.get(), timeout=timeout)
        ntype = zwargs["notificationType"]
        if ntype == "ValueAdded" and zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY":
            node_id = zwargs["nodeId"]
            switch_id = zwargs["valueId"]["id"]
            switch = Switch(node_id, switch_id)
            print("Adding %s" % switch)
            self.switches[node_id] = switch
        elif ntype == "ValueChanged" and zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY":
            node_id = zwargs["nodeId"]
            switch_id = zwargs["valueId"]["id"]
            onoff = zwargs["valueId"]["value"]
            try:
                switch = self.switches[node_id]
                if switch.switch_id != switch_id:
                    raise KeyError
            except KeyError:
                print("Unknown switch %r" % node_id)
            else:
                switch.onoff = onoff
                print(switch)
                # XXX do something here
        elif ntype == "Notification" and zwargs["notificationCode"] == 6:
            node_id = zwargs["nodeId"]
            try:
                switch = self.switches[node_id]
            except KeyError:
                pass
            else:
                # TODO: something... but only after wait_for_nodes
                print("Switch %r alive" % node_id)

        return zwargs

    async def q_passive(self, timeout):
        try:
            await self._q_get(timeout)
        except asyncio.TimeoutError:
            pass

    async def match(self, notify_type, *match):
        return await self.match_any([notify_type], *match)

    async def match_any(self, notify_types, *match):
        if match:
            note = " with %s=%r" % ("".join("[%r]" % m for m in match[:-1]), match[-1])
        else:
            note = ""
        print("=== Waiting for %r%s ===" % (notify_types, note))
        timeout = self._timeout
        while True:
            start = time.monotonic()
            zwargs = await self._q_get(timeout=timeout)
            timeout -= (time.monotonic() - start)
            if zwargs["notificationType"] not in notify_types:
                continue
            if match:
                z = zwargs
                for m in match[:-1]:
                    z = z[m]
                if z != match[-1]:
                    continue
            return zwargs


class CO2Reader:
    WINDOW_SEC = 60

    def __init__(self):
        self.samples = collections.deque()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.reader_loop()
            except asyncio.CancelledError:
                return
            except Exception as e:
                print("CO2Reader failed:", e)
                await asyncio.sleep(1.0)

    async def reader_loop(self):
        i2c = board.I2C()   # uses board.SCL and board.SDA
        scd = adafruit_scd30.SCD30(i2c)

        while True:
            while not scd.data_available:
                await asyncio.sleep(0.5)

            t = time.monotonic()
            self.samples.append((t, scd.CO2))
            while self.samples[0][0] < t - self.WINDOW_SEC:
                self.samples.popleft()

    def co2_avg(self, freshness=None):
        freshness = freshness or self.WINDOW_SEC
        t = time.monotonic()
        samples = self.samples
        if samples and samples[-1][0] >= t - freshness:
            co2_avg = int(sum(co2 for t, co2 in samples) / len(samples))
            if co2_avg < 100:
                co2_avg = 100
            if co2_avg > 2000:
                co2_avg = 2000
            return co2_avg
        else:
            return 0  # No data... this will turn off the fan.

class CO2Blinker:
    def __init__(self, co2_reader):
        self.co2_reader = co2_reader
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            await self.run_or_die()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            raise

    async def run_or_die(self):
        # Raspberry Pi red LED:
        # sudo chmod a+w /sys/class/leds/led1/brightness
        with open("/sys/class/leds/led1/brightness", "w") as f:
            async def write_n(n, sleep_time=None):
                f.write("%d\n" % n)
                f.flush()
                if sleep_time is not None:
                    await asyncio.sleep(sleep_time)
            while True:
                co2_avg = self.co2_reader.co2_avg(freshness=5.0)
                number = co2_avg // 100
                print("CO2Blinker %d -> %d" % (co2_avg, number))
                for i in range(number):
                    if (i + 1) % 5 == 0:
                        await write_n(0, 0.2)
                        await write_n(1, 0.3)
                        await write_n(0, 0.0)
                    else:
                        await write_n(0, 0.2)
                        await write_n(1, 0.1)
                        await write_n(0, 0.2)
                await asyncio.sleep(3.0)

async def hard_reset(args):
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
    manager.addWatcher(st.threadsafe_watcher_cb)
    manager.addDriver(args.device)

    await st.wait_for_nodes()

    print("Resetting controller...")
    manager.resetController(st.home_id)
    await st.wait_for_driver_removed()
    await st.wait_for_nodes()

    # XXX probably want to add N switches here?
    print("Adding node...")
    manager.addNode(st.home_id, doSecurity=False)
    zwargs = await st.match("ControllerCommand", "controllerState", "Waiting")
    print(RESET_DOC)

    await st.match("ValueAdded", "valueId", "commandClass", "COMMAND_CLASS_SWITCH_BINARY")
    await st.match("ControllerCommand", "controllerState", "Completed")

    print("Everything seems fine!")
    manager.destroy()


async def co2_main(args):
    co2_reader = CO2Reader()
    co2_blinker = CO2Blinker(co2_reader)
    try:
        await co2_sub(args, co2_reader)
    finally:
        co2_blinker.task.cancel()
        co2_reader.task.cancel()

async def co2_sub(args, co2_reader):
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
    manager.addWatcher(st.threadsafe_watcher_cb)
    manager.addDriver(args.device)

    await st.wait_for_nodes()
    print("Active switch count: %d" % len(st.switches))

    # Useful for detecting when a switch is dead.
    manager.setPollInterval(10000, True)
    for switch in st.switches.values():
        manager.enablePoll(switch.switch_id)

    async def wait_for_quiet():
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
            await asyncio.sleep(0.1)

    onoff = False

    while True:
        co2 = co2_reader.co2_avg()
        if co2 > 800:
            onoff = True
        elif co2 < 750:
            onoff = False

        await wait_for_quiet()
        for switch in st.switches.values():
            manager.setValue(switch.switch_id, 1 if onoff else 0)

        ts = datetime.datetime.now().replace(microsecond=0).isoformat(" ")
        print("%s, %d, %d" % (ts, co2, onoff), flush=True)

        # Passively consume messages for a while.
        start = time.monotonic()
        while time.monotonic() < start + 10:
            await st.q_passive(1.0)

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
        asyncio.run(hard_reset(args))
    elif args.co2:
        asyncio.run(co2_main(args))

if __name__ == '__main__':
    main()

