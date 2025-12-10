import socket
from typing import Optional
from cat.icat import ICat
import logging as L


logger = L.getLogger(__name__)


class rigctld(ICat):

    def init_cat(self, **kwargs):
        '''
        Initializes the RIGCTLD CAT control interface.

        :param: **kwargs
            keywords required:
                host = string ip address
                port = integer port number
        '''
        self.host = kwargs['host']
        self.port = kwargs['port']

        try:
            self.socket = socket.socket()
            self.socket.settimeout(0.5)
            self.socket.connect((self.host, self.port))
            logger.info(f"Connected to rigctrld - {self.host}:{self.port}")
            self.online = True
        except (socket.timeout, socket.error) as e:
            self.socket = None
            self.online = False
            logger.warning("init_cat", exc_info=e)

    def _mode_passband(self, mode: str) -> int:
        """
        Determine the passband width (Hz) that should accompany a mode change.

        Default rigctld behaviour when width=0 is to keep whatever bandwidth the
        VFO already had selected, which is why the rig ends up in a generic 3 kHz
        filter when we jump to a new SSB spot.  Provide reasonable defaults here.
        """
        normalized = (mode or "").upper()
        if normalized in ("USB", "LSB", "USB-D", "LSB-D"):
            return 2400  # tighter SSB filter for hunting voice spots
        if normalized.startswith("CW"):
            return 500
        if normalized in ("DIGU", "DIGL"):
            return 3000  # let data modes breathe a little
        return 0  # fall back to rig default

    def set_mode(self, mode: str, bandwidth: Optional[int] = None) -> bool:
        """sets the radios mode"""
        if self.socket:
            try:
                self.online = True
                width = bandwidth if bandwidth is not None else self._mode_passband(mode)
                self.socket.send(bytes(f"M {mode} {width}\n", "utf-8"))
                _ = self.socket.recv(1024).decode().strip()
                return True
            except socket.error as e:
                self.online = False
                logger.debug("set_mode", exc_info=e)
                self.socket = None
                return False

        self.init_cat(host=self.host, port=self.port)
        return False

    def set_vfo(self, freq: str) -> bool:
        """sets the radios vfo"""
        if self.socket:
            try:
                self.online = True
                self.socket.send(bytes(f"F {freq}\n", "utf-8"))
                _ = self.socket.recv(1024).decode().strip()
                return True
            except socket.error as e:
                self.online = False
                logger.debug("set_vfo", exc_info=e)
                self.socket = None
                return False

        self.init_cat(host=self.host, port=self.port)
        return False

    def get_ptt(self):
        """Returns ptt state via rigctld"""
        if self.socket:
            try:
                self.online = True
                self.socket.send(b"t\n")
                ptt = self.socket.recv(1024).decode()
                logger.debug("%s", ptt)
                ptt = ptt.strip()
                return ptt
            except socket.error as exception:
                self.online = False
                logger.debug("%s", exception)
                self.socket = None
        return "0"
