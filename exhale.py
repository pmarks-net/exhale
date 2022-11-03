#!/usr/bin/env python3

import argparse
import asyncio
import collections
import datetime
import math
import tempfile
import threading
import time
import traceback
from enum import Enum

import libopenzwave
from openzwave.option import ZWaveOption

# pip3 install adafruit-circuitpython-scd30
import board
import busio
import adafruit_scd30

# pip3 install adafruit-extended-bus
# dtoverlay=i2c-gpio,bus=6,i2c_gpio_scl=9,i2c_gpio_sda=10
import adafruit_extended_bus

RESET_DOC = """
Tips for my UltraPro Z-Wave toggle switch:

Factory reset (run --hard_reset *after* this step):
    Quickly press up up up down down down.

Join the network:
    Press up.
"""

class SwitchState(Enum):
    ALIVE = 1
    ON = 2
    OFF = 3
    WANT_ON = 4
    WANT_OFF = 5


class SwitchAlive(Exception):
    pass


class SwitchToggled(Exception):
    pass


class Switch:
    def __init__(self, node_id, switch_id, manager_set_value):
        self.node_id = node_id
        self.switch_id = switch_id
        self.manager_set_value = manager_set_value
        self.onoff = False
        self.want_onoff = None
        self.task = asyncio.create_task(self.run())

        # Queue of SwitchState enums.
        self.q = asyncio.Queue()

    def __str__(self):
        return "Switch node_id=%r, switch_id=%r, onoff=%r" % (self.node_id, self.switch_id, self.onoff)

    def set_alive(self):
        self.q.put_nowait(SwitchState.ALIVE)

    def set_onoff(self, v):
        if v:
            self.q.put_nowait(SwitchState.ON)
        else:
            self.q.put_nowait(SwitchState.OFF)

    def set_want_onoff(self, v):
        if v:
            self.q.put_nowait(SwitchState.WANT_ON)
        else:
            self.q.put_nowait(SwitchState.WANT_OFF)

    async def run(self):
        try:
            await self.run_or_die()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            raise

    async def run_or_die(self):
        # Wait for first ALIVE.
        try:
            while True:
                print("Waiting for ALIVE")
                await self.eat_q(duration=None)
        except SwitchAlive:
            pass
        while True:
            try:
                await self.alive_loop()
            except SwitchAlive:
                print("Rebooting alive_loop")

    async def alive_loop(self):
        # How long to ignore manual toggles after a state change.
        DEBOUNCE = 5.0  # seconds

        try:
            # Send (off 1sec, on 1sec, off) to tell humans that the switch is
            # under automatic control.
            print("Sending ALIVE pulse")
            await self.send_and_ignore(False, 1.0)
            await self.send_and_ignore(True, 1.0)
            await self.send_and_debounce(False, DEBOUNCE)

            while True:
                # Control the switch automatically.
                if self.want_onoff not in (None, self.onoff):
                    await self.send_and_debounce(self.want_onoff, DEBOUNCE)

                # Wait for humans to mess with the switch.
                await self.eat_q(duration=None, monitor_toggled=True)

        except SwitchToggled:
            print("Begin manual override")
            while True:
                try:
                    # XXX make this an argument?
                    await self.eat_q(duration=3600.0, monitor_toggled=True)
                except SwitchToggled:
                    print("Restart manual override")
                    continue
                else:
                    print("Back to automatic control")
                    break

    async def send_and_ignore(self, value, duration):
        print("send_and_ignore", value, duration)
        self.manager_set_value(self.switch_id, value)
        await self.eat_q(duration=duration)

    async def send_and_debounce(self, value, duration):
        print("send_and_debounce", value, duration)
        self.manager_set_value(self.switch_id, value)
        # Wait for the state to settle.
        await self.eat_q(duration=duration)
        # If it settled to the wrong value, blame the human.
        if self.onoff != value:
            raise SwitchToggled

    async def eat_q(self, duration, monitor_toggled=False):
        if duration is None:
            # Wait indefinitely for the first event,
            # then stop as soon as the queue is empty.
            stop_on_empty = True
        else:
            # Wait until the duration expires.
            wait_until = time.monotonic() + duration
            stop_on_empty = False

        alive = False
        toggled = False

        while True:
            try:
                v = await asyncio.wait_for(self.q.get(), duration)
                #print("eat_q v=", v)
            except asyncio.TimeoutError:
                #print("eat_q timeout", alive, toggled)
                if alive:
                    raise SwitchAlive
                if toggled:
                    raise SwitchToggled
                return

            if v == SwitchState.ALIVE:
                alive = True
                stop_on_empty = True
            elif v in (SwitchState.ON, SwitchState.OFF):
                onoff = (v == SwitchState.ON)
                #print("onoff=%r" % onoff)
                if self.onoff != onoff:
                    self.onoff = onoff
                    if monitor_toggled:
                        print("TOGGLED!")
                        toggled = True
                        stop_on_empty = True
            elif v in (SwitchState.WANT_ON, SwitchState.WANT_OFF):
                self.want_onoff = (v == SwitchState.WANT_ON)
                #print("want_onoff=%r" % self.want_onoff)

            if stop_on_empty:
                duration = 0
            else:
                duration = wait_until - time.monotonic()


class StateTracker:
    def __init__(self, manager_set_value):
        self._manager_set_value = manager_set_value
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self.switches = {}
        self.home_id = None
        self.nodes_queried = False

    def threadsafe_watcher_cb(self, zwargs):
        #print(f"zwave event: {datetime.datetime.now().isoformat(sep=' ')} {zwargs}")
        self._loop.call_soon_threadsafe(lambda: self._q.put_nowait(zwargs))

    async def wait_for_nodes(self):
        if self.home_id is not None:
            raise AssertionError("Can't wait_for_nodes() with existing home_id")
        zwargs = await self._match("DriverReady")
        self.home_id = zwargs['homeId']
        await self._match("AllNodesQueried|AllNodesQueriedSomeDead")
        self.nodes_queried = True
        for switch in self.switches.values():
            switch.set_alive()

    async def wait_for_driver_removed(self):
        await self._match("DriverRemoved")
        self.home_id = None
        self.nodes_queried = False
        for switch in self.switches.values():
            switch.task.cancel()
        self.switches.clear()

    async def wait_for_controller_state(self, cs):
        return await self._match("ControllerCommand", f"controllerState={cs}")

    async def wait_for_switch_added(self):
        zwargs = await self._match(
                "ValueAdded",
                "valueId.commandClass=COMMAND_CLASS_SWITCH_BINARY",
                timeout=15*60)  # Wait 15 minutes for user to add the switch.
        return zwargs["valueId"]["id"]

    async def wait_until(self, mono_ts):
        while True:
            timeout = mono_ts - time.monotonic()
            if timeout <= 0:
                break
            try:
                await self._q_get(timeout)
            except asyncio.TimeoutError:
                pass

    # notify_types = "Type1|Type2|..."
    # prop_chain = "a.b.c=value"
    async def _match(self, notify_types, prop_chain=None, timeout=60):
        notify_types = notify_types.split("|")
        note =  f" with {prop_chain}" if prop_chain else ""
        print(f"=== Waiting for {notify_types}{note} ===")
        while True:
            start = time.monotonic()
            zwargs = await self._q_get(timeout=timeout)
            timeout -= (time.monotonic() - start)
            if zwargs["notificationType"] not in notify_types:
                continue
            if prop_chain:
                props, value = prop_chain.split("=")
                z = zwargs
                for m in props.split("."):
                    z = z[m]
                if z != value:
                    continue
            return zwargs

    async def _q_get(self, timeout):
        zwargs = await asyncio.wait_for(self._q.get(), timeout=timeout)
        self._q.task_done()

        # Check for events that we're always waiting for.
        ntype = zwargs["notificationType"]
        if ntype == "ValueAdded" and zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY":
            node_id = zwargs["nodeId"]
            switch_id = zwargs["valueId"]["id"]
            switch = Switch(node_id, switch_id, self._manager_set_value)
            try:
                self.switches[node_id].task.cancel()
                print("Destroyed duplicate switch with node_id %r" % node_id)
            except KeyError:
                pass
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
                switch.set_onoff(onoff)
        elif ntype == "Notification" and zwargs["notificationCode"] == 6:
            node_id = zwargs["nodeId"]
            try:
                switch = self.switches[node_id]
            except KeyError:
                pass
            else:
                if self.nodes_queried:
                    switch.set_alive()
                print("Switch %r alive" % node_id)

        return zwargs


class Averager:
    def __init__(self, twindow):
        self.q = collections.deque()
        self.twindow = twindow  # Average over this time window (seconds)

    def add(self, now, value):
        if self.q and self.q[0][0] > now:
            raise AssertionError("must use time.monotonic()")

        # Add new value, and purge values older than twindow.
        self.q.appendleft((now, value))
        while self.q[-1][0] <= now - self.twindow:
            self.q.pop()

    def is_fresh(self, now):
        # Is the latest value still within the window?
        return self.q and self.q[0][0] > now - self.twindow

    def compute_avg(self):
        return sum(value for ts, value in self.q) / (len(self.q) or 1)


class CO2Reader:
    def __init__(self, blinker):
        self.blinker = blinker
        self.avgr = Averager(60)
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
        #i2c = board.I2C()   # uses board.SCL and board.SDA
        i2c = adafruit_extended_bus.ExtendedI2C(6)
        scd = adafruit_scd30.SCD30(i2c)

        while True:
            while not scd.data_available:
                await asyncio.sleep(0.5)

            now = time.monotonic()
            self.avgr.add(now, scd.CO2)

            co2_avg = self.compute_co2_avg()
            self.blinker.blink_number(co2_avg // 100)

    def compute_co2_avg(self):
        now = time.monotonic()
        if self.avgr.is_fresh(now):
            co2_avg = int(self.avgr.compute_avg())
            # Enforce reasonable limits for the blinker.
            if co2_avg < 100:
                co2_avg = 100
            if co2_avg > 2000:
                co2_avg = 2000
            return co2_avg
        else:
            return 0  # No data... this will turn off the fan.


class Blinker:
    def __init__(self):
        self.q = asyncio.Queue(maxsize=1)
        self.task = asyncio.create_task(self.run())

    def blink_number(self, number):
        try:
            self.q.put_nowait(number)
        except asyncio.QueueFull:
            pass

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
                number = await self.q.get()
                #print(f"blink {number}")
                self.q.task_done()
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

async def hard_reset(args, user_path):
    print("hard_reset")

    options = ZWaveOption(device=args.device, user_path=user_path)
    options.set_log_file("/dev/null")
    options.set_console_output(False)
    options.lock()

    def manager_set_value(switch_id, value):
        print("ignored manager_set_value")

    st = StateTracker(manager_set_value)

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
    await st.wait_for_controller_state("Waiting")
    print(RESET_DOC)

    switch_id = await st.wait_for_switch_added()
    # Acknowledge the new switch, by turning it off.
    manager.setValue(switch_id, False)
    await st.wait_for_controller_state("Completed")

    print("Everything seems fine!")
    manager.destroy()


async def co2_main(args, user_path):
    blinker = Blinker()
    co2_reader = CO2Reader(blinker)
    try:
        await co2_sub(args, user_path, co2_reader)
    finally:
        blinker.task.cancel()
        co2_reader.task.cancel()

async def co2_sub(args, user_path, co2_reader):
    options = ZWaveOption(device=args.device, user_path=user_path)
    options.set_log_file("/dev/null")
    options.set_console_output(False)
    options.lock()

    def manager_set_value(switch_id, value):
        manager.setValue(switch_id, value)

    st = StateTracker(manager_set_value)

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

    onoff = False

    duty_1h_avgr = Averager(1*3600)
    duty_24h_avgr = Averager(24*3600)

    while True:
        co2 = co2_reader.compute_co2_avg()
        if co2 > args.co2_limit:
            onoff = True
        elif co2 < args.co2_limit - 50:  # XXX configurable?
            onoff = False

        for switch in st.switches.values():
            switch.set_want_onoff(onoff)

        now = time.monotonic()
        duty_1h_avgr.add(now, onoff)
        duty_24h_avgr.add(now, onoff)

        # Round up, so any activity reports >= 1%
        duty_1h = math.ceil(duty_1h_avgr.compute_avg() * 100)
        duty_24h = math.ceil(duty_24h_avgr.compute_avg() * 100)

        ts = datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")
        print(f"{ts} co2={co2} onoff={int(onoff)} duty_1h={duty_1h}% duty_24h={duty_24h}%", flush=True)

        # Passively consume messages for a while.
        await st.wait_until(now + 10)

def pyozw_parser():
    parser = argparse.ArgumentParser(description='XXX')
    parser.add_argument('-d', '--device', action='store', help='The device port', default=None)
    parser.add_argument('-t', '--timeout', action='store',type=int, help='The default timeout for zwave network sniffing', default=None)
    parser.add_argument('--hard_reset', action='store_true', help='XXX', default=False)
    parser.add_argument('--co2', action='store_true', help='XXX', default=False)
    parser.add_argument('--co2_limit', type=int, action='store', help='Enable fans when CO2 exceeds this value', default=800)
    return parser

def main():
    with tempfile.TemporaryDirectory(prefix="exhale-userpath-") as user_path:
        parser = pyozw_parser()
        args = parser.parse_args()
        if args.hard_reset:
            asyncio.run(hard_reset(args, user_path))
        elif args.co2:
            asyncio.run(co2_main(args, user_path))
        else:
            raise AssertionError("Usage: XXX")

if __name__ == '__main__':
    main()

