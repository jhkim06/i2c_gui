# i2c_rpi_helper.py
from __future__ import annotations

from math import ceil
from typing import Union
import time

from smbus2 import SMBus, i2c_msg
# ~/tamalero/lib/python3.8/site-packages/i2c_gui2 
from i2c_gui2.i2c_connection_helper import I2C_Connection_Helper
from i2c_gui2.i2c_messages import I2CMessages


class RPI_I2C_Helper(I2C_Connection_Helper):
    """
    Raspberry Pi / Linux i2c-dev backend for I2C_Connection_Helper using smbus2.

    Notes:
      - Pi bus speed is configured by Linux/device-tree, not per-transaction.
      - _direct_i2c() here supports the common patterns used by this project, but
        it cannot reproduce *arbitrary* START/STOP/ACK timing the way USB-ISS can.
    """

    def __init__(
        self,
        bus: int = 1,
        max_seq_byte: int = 32,
        dummy_connect: bool = False,
        successive_i2c_delay_us: int = 10000,
    ):
        super().__init__(max_seq_byte=max_seq_byte, successive_i2c_delay_us=successive_i2c_delay_us, no_connect=dummy_connect)

        self._bus_id = bus
        self._bus: Union[SMBus, None] = None

        if not dummy_connect:
            self._bus = SMBus(self._bus_id)

        self._is_connected = True

    def __del__(self):
        try:
            if self._bus is not None:
                self._bus.close()
        except Exception:
            pass

    # --- Required low-level implementations ---

    def _check_i2c_device(self, device_address: int) -> bool:
        if self._no_connect:
            return True

        # SMBus "quick" is the closest thing to an address probe.
        # If the device NACKs, Linux raises OSError.
        try:
            assert self._bus is not None
            self._bus.write_quick(device_address)
            return True
        except OSError:
            return False

    def _write_i2c_device_memory(
        self,
        device_address: int,
        word_address: int,
        byte_data: list[int],
        write_type: str = "Normal",
        address_bitlength: int = 8,
    ):
        if self._no_connect:
            return

        assert self._bus is not None

        addr_nbytes = ceil(address_bitlength / 8)
        addr_bytes = list(word_address.to_bytes(addr_nbytes, byteorder="big", signed=False))

        # Most register-memory devices expect: [reg_addr_bytes..., data...]
        payload = addr_bytes + list(byte_data)

        msg = i2c_msg.write(device_address, payload)
        self._bus.i2c_rdwr(msg)

    def _read_i2c_device_memory(
        self,
        device_address: int,
        word_address: int,
        byte_count: int,
        read_type: str = "Normal",
        address_bitlength: int = 8,
    ) -> list[int]:
        if self._no_connect:
            return [0] * byte_count

        assert self._bus is not None

        addr_nbytes = ceil(address_bitlength / 8)
        addr_bytes = list(word_address.to_bytes(addr_nbytes, byteorder="big", signed=False))

        # Typical repeated-start register read:
        #   write(register-address-bytes), then read(byte_count)
        w = i2c_msg.write(device_address, addr_bytes)
        r = i2c_msg.read(device_address, byte_count)
        self._bus.i2c_rdwr(w, r)

        return list(r)

    def _direct_i2c(self, commands: list[I2CMessages]) -> list[int]:
        """
        The project's I2CMessages enum is modeled on USB-ISS command bytes.
        Linux i2c-dev can't emulate completely arbitrary START/STOP/ACK control.

        This implementation supports the common pattern:
          START, WRITE<n> (raw bytes...), (RESTART), READ<m>, (NACK), STOP

        If your GUI uses more exotic sequences, we can extend this parser.
        """
        if self._no_connect:
            return []

        assert self._bus is not None

        # Parse into one combined transaction: one write chunk then one read chunk.
        # (This matches repeated-start semantics with i2c_rdwr)
        write_bytes: list[int] = []
        read_len = 0

        i = 0
        while i < len(commands):
            c = commands[i]

            # ignore START/STOP/RESTART/ACK/NACK markers (we'll do combined transaction)
            if c in (I2CMessages.START, I2CMessages.STOP, I2CMessages.RESTART, I2CMessages.ACK, I2CMessages.NACK):
                i += 1
                continue

            # WRITEk commands: next k bytes are literal payload
            if c.name.startswith("WRITE"):
                # e.g. WRITE3 -> take next 3 items
                k = int(c.name.replace("WRITE", ""))
                # next k entries should be integer-like (or I2CMessages holding raw byte values)
                for j in range(1, k + 1):
                    b = commands[i + j]
                    write_bytes.append(int(b))
                i += (k + 1)
                continue

            # READk commands
            if c.name.startswith("READ"):
                k = int(c.name.replace("READ", ""))
                read_len += k
                i += 1
                continue

            raise RuntimeError(f"Unsupported I2C direct command: {c} (index {i})")

        # IMPORTANT: device address is not part of commands[] in this abstraction.
        # In this project, direct_i2c is usually used *after* the address byte was included
        # inside the WRITE payload for USB-ISS.
        #
        # If your direct_i2c usage includes the address byte explicitly, you must decide
        # how to pass it on Linux i2c-dev (which expects the 7-bit address separately).
        raise RuntimeError(
            "RPI_I2C_Helper._direct_i2c() needs the target 7-bit device address. "
            "If your code uses direct_i2c(), paste that call site and I’ll adapt this properly."
        )
if __name__=='__main__':

    print("RPI_I2C_Helper test...")
    lcd_addr = 0x27
    i2c = RPI_I2C_Helper(bus=1)
    print(i2c.check_i2c_device(lcd_addr))

    while True:
        i2c.write_device_memory(
            device_address=lcd_addr,
            word_address=0,
            data=[0x08],
            address_bitlength=0,
        )
        time.sleep(1)
        i2c.write_device_memory(
            device_address=lcd_addr,
            word_address=0,
            data=[0x00],
            address_bitlength=0,
        )
        time.sleep(1)

