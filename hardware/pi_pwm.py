# -*- coding: utf-8 -*-
"""
A module to control the QO Raspberry Pi based H-Bridge hardware.

Qudi is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Qudi is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Qudi. If not, see <http://www.gnu.org/licenses/>.

Copyright (c) the Qudi Developers. See the COPYRIGHT.txt file at the
top-level directory of this distribution and at <https://github.com/Ulm-IQO/qudi/>
"""


from core.base import Base
from interface.process_control_interface import ProcessControlInterface
from core.util.mutex import Mutex

import RPi.GPIO as GPIO


class PiPWM(Base, ProcessControlInterface):
    _modclass = 'ProcessControlInterface'
    _modtype = 'hardware'

    ## declare connectors
    _out = {'pwm': 'ProcessControlInterface'}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        #locking for thread safety
        self.threadlock = Mutex()

    def on_activate(self, e):
        config = self.getConfiguration()

        if 'channel' in config:
            channel = config['channel']
        else:
            channel = 0
            self.log.warning('PWN channel not set, using 0')

        # pin mapping
        if channel == 0:
            self.inapin = 5
            self.inbpin = 22
            self.pwmpin = 24
            self.enapin = 25
            self.enbpin = 23
            self.diapin = 17
            self.dibpin = 18
            self.fanpin = 27
        else:
            self.inapin = 16
            self.inbpin = 19
            self.pwmpin = 21
            self.enapin = 20
            self.enbpin = 26
            self.diapin = 12
            self.dibpin = 6
            self.fanpin = 13

        if 'frequency' in config:
            self.freq = config['frequency']
        else:
            self.freq = 100
            self.log.warning('Frequency not set, using 100Hz.')
        self.setupPins()
        self.startPWM()

    def on_deactivate(self, e):
        self.stopPWM()

    def setupPins(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.enapin, GPIO.OUT)
        GPIO.setup(self.enbpin, GPIO.OUT)
        GPIO.setup(self.inapin, GPIO.OUT)
        GPIO.setup(self.inbpin, GPIO.OUT)
        GPIO.setup(self.pwmpin, GPIO.OUT)
        GPIO.setup(self.fanpin, GPIO.OUT)
        GPIO.setup(self.diapin, GPIO.IN)
        GPIO.setup(self.dibpin, GPIO.IN)
        self.p = GPIO.PWM(self.pwmpin, self.freq)

    def startPWM(self):
        GPIO.output(self.fanpin, True)
        GPIO.output(self.enapin, True)
        GPIO.output(self.enbpin, True)
        GPIO.output(self.inapin, True)
        GPIO.output(self.inbpin, False)

        # Setup PWM and DMA channel 0
        self.p.start(0)
        self.dutycycle = 0

    def stopPWM(self):
        # Stop PWM
        self.p.stop()
        GPIO.output(self.enapin, False)
        GPIO.output(self.enbpin, False)
        GPIO.output(self.inapin, False)
        GPIO.output(self.inbpin, False)
        GPIO.output(self.fanpin, False)

    def changeDutyCycle(self, duty):
        self.dutycycle = 0
        if duty >= 0:
            GPIO.output(self.inapin, True)
            GPIO.output(self.inbpin, False)
        else:
            GPIO.output(self.inapin, False)
            GPIO.output(self.inbpin, True)
        self.p.ChangeDutyCycle(abs(duty))

    def setControlValue(self, value):
        with self.threadlock:
            self.changeDutyCycle(value)

    def getControlValue(self):
        return self.dutycycle

    def getControlUnit(self):
        return ('%', 'percent')

    def getControlLimits(self):
        return (-100, 100)


class PiPWMHalf(PiPWM):
    def __init__(self, manager, name, config = None, **kwargs):
        if config is None:
            config = {}
        c_dict = {'onactivate': self.activation, 'ondeactivate': self.deactivation}
        PiPWM.__init__(self, manager, name, **kwargs)
        #locking for thread safety
        self.threadlock = Mutex()

    def getControlLimits(self):
        return (0, 100)
