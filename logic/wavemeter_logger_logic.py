# -*- coding: utf-8 -*-

"""
This file contains the logic responsible for coordinating laser scanning.

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


from qtpy import QtCore
from collections import OrderedDict
import numpy as np
import time
import datetime
import matplotlib as mpl
import matplotlib.pyplot as plt

from logic.generic_logic import GenericLogic
from core.util.mutex import Mutex


class HardwarePull(QtCore.QObject):

    """ Helper class for running the hardware communication in a separate thread. """

    def __init__(self, parentclass):
        super().__init__()

        # remember the reference to the parent class to access functions ad settings
        self._parentclass = parentclass

    def handle_timer(self, state_change):
        """ Threaded method that can be called by a signal from outside to start the timer.

        @param bool state: (True) starts timer, (False) stops it.
        """

        if state_change:
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._update_data)
            self.timer.start(self._parentclass._logic_acquisition_timing)
        else:
            if hasattr(self, 'timer'):
                self.timer.stop()

    def _update_data(self):
        """ This method gets the count data from the hardware.
            It runs repeatedly in the logic module event loop by being connected
            to sigCountNext and emitting sigCountNext through a queued connection.
        """

        hardware = self._parentclass._wavemeter_device
        self._parentclass.current_wavelength = 1.0 * hardware.get_current_wavelength()

        time_stamp = time.time() - self._parentclass._acqusition_start_time

        # only wavelength >200 nm make sense, ignore the rest
        if self._parentclass.current_wavelength > 200:
            self._parentclass._wavelength_data.append(
                np.array([time_stamp, self._parentclass.current_wavelength])
            )

        # check if we have a new min or max and save it if so
        if self._parentclass.current_wavelength > self._parentclass.intern_xmax:
            self._parentclass.intern_xmax = self._parentclass.current_wavelength
        if self._parentclass.current_wavelength < self._parentclass.intern_xmin:
            self._parentclass.intern_xmin = self._parentclass.current_wavelength

        if (
            (not self._parentclass._counter_logic.get_saving_state()) or
            self._parentclass._counter_logic.getState() == 'idle'
        ):

            self._parentclass.stop_scanning()


class WavemeterLoggerLogic(GenericLogic):

    """This logic module gathers data from wavemeter and the counter logic.
    """

    sig_data_updated = QtCore.Signal()
    sig_update_histogram_next = QtCore.Signal(bool)
    sig_handle_timer = QtCore.Signal(bool)
    sig_new_data_point = QtCore.Signal(list)

    _modclass = 'laserscanninglogic'
    _modtype = 'logic'

    # declare connectors
    _in = {'wavemeter1': 'WavemeterInterface',
           'savelogic': 'SaveLogic',
           'counterlogic': 'CounterLogic'
           }
    _out = {'wavemeterloggerlogic': 'WavemeterLoggerLogic'}

    def __init__(self, config, **kwargs):
        """ Create WavemeterLoggerLogic object with connectors.

          @param dict config: module configuration
          @param dict kwargs: optional parameters
        """
        super().__init__(config=config, **kwargs)

        # locking for thread safety
        self.threadlock = Mutex()

        if 'logic_acquisition_timing' in config.keys():
            self._logic_acquisition_timing = config['logic_acquisition_timing']
        else:
            self._logic_acquisition_timing = 20.
            self.log.warning('No logic_acquisition_timing configured, '
                             'using {} instead.'.format(self._logic_acquisition_timing)
                             )

        if 'logic_update_timing' in config.keys():
            self._logic_update_timing = config['logic_update_timing']
        else:
            self._logic_update_timing = 100.
            self.log.warning('No logic_update_timing configured, '
                             'using {} instead.'.format(self._logic_update_timing)
                             )

        self._acqusition_start_time = 0
        self._bins = 200
        self._data_index = 0

        self._recent_wavelength_window = [0, 0]
        self.counts_with_wavelength = []

        self._xmin = 650
        self._xmax = 750
        # internal min and max wavelength determined by the measured wavelength
        self.intern_xmax = -1.0
        self.intern_xmin = 1.0e10
        self.current_wavelength = 0

    def on_activate(self, e):
        """ Initialisation performed during activation of the module.

          @param object e: Fysom state change event
        """
        self._wavelength_data = []

        self.stopRequested = False

        self._wavemeter_device = self.get_in_connector('wavemeter1')
#        print("Counting device is", self._counting_device)

        self._save_logic = self.get_in_connector('savelogic')
        self._counter_logic = self.get_in_connector('counterlogic')

        # create a new x axis from xmin to xmax with bins points
        self.histogram_axis = np.arange(self._xmin,
                                        self._xmax,
                                        (self._xmax - self._xmin) / self._bins
                                        )
        self.histogram = np.zeros(self.histogram_axis.shape)
        self.envelope_histogram = np.zeros(self.histogram_axis.shape)

        self.sig_update_histogram_next.connect(self._attach_counts_to_wavelength,
                                               QtCore.Qt.QueuedConnection
                                               )

        # create an indepentent thread for the hardware communication
        self.hardware_thread = QtCore.QThread()

        # create an object for the hardware communication and let it live on the new thread
        self._hardware_pull = HardwarePull(self)
        self._hardware_pull.moveToThread(self.hardware_thread)

        # connect the signals in and out of the threaded object
        self.sig_handle_timer.connect(self._hardware_pull.handle_timer)

        # start the event loop for the hardware
        self.hardware_thread.start()
        self.last_point_time = time.time()

    def on_deactivate(self, e):
        """ Deinitialisation performed during deactivation of the module.

          @param object e: Fysom state change event
        """
        if self.getState() != 'idle' and self.getState() != 'deactivated':
            self.stop_scanning()
        self.hardware_thread.quit()
        self.sig_handle_timer.disconnect()

    def get_max_wavelength(self):
        return self._xmax

    def get_min_wavelength(self):
        return self._xmin

    def get_bins(self):
        return self._bins

    def recalculate_histogram(self, bins=None, xmin=None, xmax=None):
        if bins is not None:
            self._bins = bins
        if xmin is not None:
            self._xmin = xmin
        if xmax is not None:
            self._xmax = xmax

#        print('New histogram', self._bins,self._xmin,self._xmax)
        # create a new x axis from xmin to xmax with bins points
        self.rawhisto = np.zeros(self._bins)
        self.envelope_histogram = np.zeros(self._bins)
        self.sumhisto = np.ones(self._bins) * 1.0e-10
        self.histogram_axis = np.linspace(self._xmin, self._xmax, self._bins)
        self.sig_update_histogram_next.emit(True)

    def start_scanning(self, resume=False):
        """ Prepare to start counting:
            zero variables, change state and start counting "loop"
        """

        self.run()

        if self._counter_logic.getState() == 'idle':
            self._counter_logic.startCount()

        if self._counter_logic.get_saving_state():
            self._counter_logic.save_data()

        self._wavemeter_device.start_acqusition()

        self._counter_logic.start_saving(resume=resume)

        if not resume:
            self._acqusition_start_time = self._counter_logic._saving_start_time
            self._wavelength_data = []

            self.data_index = 0

            self._recent_wavelength_window = [0, 0]
            self.counts_with_wavelength = []

            self.rawhisto = np.zeros(self._bins)
            self.sumhisto = np.ones(self._bins) * 1.0e-10
            self.intern_xmax = -1.0
            self.intern_xmin = 1.0e10
            self.recent_avg = [0, 0, 0]
            self.recent_count = 0

        # start the measuring thread
        self.sig_handle_timer.emit(True)
        self._complete_histogram = True
        self.sig_update_histogram_next.emit(False)

        return 0

    def stop_scanning(self):
        """ Set a flag to request stopping counting.
        """

        if not self.getState() == 'idle':
            # self._wavemeter_device.stop_acqusition()
            # stop the measurement thread
            self.sig_handle_timer.emit(False)
            # set status to idle again
            self.stop()

        if self._counter_logic.get_saving_state():
            self._counter_logic.save_data(to_file=False)

        return 0

    def _attach_counts_to_wavelength(self, complete_histogram):
        """ Interpolate a wavelength value for each photon count value.  This process assumes that
        the wavelength is varying smoothly and fairly continuously, which is sensible for most
        measurement conditions.

        Recent count values are those recorded AFTER the previous stitch operation, but BEFORE the
        most recent wavelength value (do not extrapolate beyond the current wavelength
        information).
        """

        # If there is not yet any wavelength data, then wait and signal next loop
        if len(self._wavelength_data) == 0:
            time.sleep(self._logic_update_timing * 1e-3)
            self.sig_data_updated.emit()
            return

        # The end of the recent_wavelength_window is the time of the latest wavelength data
        self._recent_wavelength_window[1] = self._wavelength_data[-1][0]

        # (speed-up) We only need to worry about "recent" counts, because as the count data gets
        # very long all the earlier points will already be attached to wavelength values.
        count_recentness = 100  # TODO: calculate this from count_freq and wavemeter refresh rate

        # TODO: Does this depend on things, or do we loop fast enough to get every wavelength value?
        wavelength_recentness = np.min([5, len(self._wavelength_data)])

        recent_counts = np.array(self._counter_logic._data_to_save[-count_recentness:])
        recent_wavelengths = np.array(self._wavelength_data[-wavelength_recentness:])

        # The latest counts are those recorded during the recent_wavelength_window
        count_idx = [0, 0]
        count_idx[0] = np.searchsorted(recent_counts[:, 0], self._recent_wavelength_window[0])
        count_idx[1] = np.searchsorted(recent_counts[:, 0], self._recent_wavelength_window[1])

        latest_counts = recent_counts[count_idx[0]:count_idx[1]]

        # Interpolate to obtain wavelength values at the times of each count
        interpolated_wavelengths = np.interp(latest_counts[:, 0],
                                             xp=recent_wavelengths[:, 0],
                                             fp=recent_wavelengths[:, 1]
                                             )

        # Stitch interpolated wavelength into latest counts array
        latest_stitched_data = np.insert(latest_counts, 2, values=interpolated_wavelengths, axis=1)

        # Add this latest data to the list of counts vs wavelength
        self.counts_with_wavelength += latest_stitched_data.tolist()

        # The start of the recent data window for the next round will be the end of this one.
        self._recent_wavelength_window[0] = self._recent_wavelength_window[1]

        # Run the old update histogram method to keep duplicate data
        self._update_histogram(complete_histogram)

        # Signal that data has been updated
        self.sig_data_updated.emit()

        # Wait and repeat if measurement is ongoing
        time.sleep(self._logic_update_timing * 1e-3)

        if self.getState() == 'running':
            self.sig_update_histogram_next.emit(False)

    def _update_histogram(self, complete_histogram):
        """ Calculate new points for the histogram.

        @param bool complete_histogram: should the complete histogram be recalculated, or just the
                                        most recent data?
        @return:
        """

        # If things like num_of_bins have changed, then recalculate the complete histogram
        # Note: The histogram may be recalculated (bins changed, etc) from the stitched data.
        # There is no need to recompute the interpolation for the stitched data.
        if complete_histogram:
            count_window = len(self._counter_logic._data_to_save)
            self._data_index = 0
            self.log.info('Recalcutating Laser Scanning Histogram for: '
                          '{0:d} counts and {1:d} wavelength.'.format(
                              count_window,
                              len(self._wavelength_data)
                          )
                          )
        else:
            count_window = min(100, len(self._counter_logic._data_to_save))

        if count_window < 2:
            time.sleep(self._logic_update_timing * 1e-3)
            self.sig_update_histogram_next.emit(False)
            return

        temp = np.array(self._counter_logic._data_to_save[-count_window:])

        # only do something if there is wavelength data to work with
        if len(self._wavelength_data) > 0:

            for i in self._wavelength_data[self._data_index:]:
                self._data_index += 1

                if i[1] < self._xmin or i[1] > self._xmax:
                    continue

                # calculate the bin the new wavelength needs to go in
                newbin = np.digitize([i[1]], self.histogram_axis)[0]
                # if the bin make no sense, start from the beginning
                if newbin > len(self.rawhisto) - 1:
                    continue

                # sum the counts in rawhisto and count the occurence of the bin in sumhisto
                interpolation = np.interp(i[0], xp=temp[:, 0], fp=temp[:, 1])
                self.rawhisto[newbin] += interpolation
                self.sumhisto[newbin] += 1.0

                self.envelope_histogram[newbin] = np.max([interpolation,
                                                          self.envelope_histogram[newbin]
                                                          ])

                datapoint = [i[1], i[0], interpolation]
                if time.time() - self.last_point_time > 1:
                    self.sig_new_data_point.emit(self.recent_avg)
                    self.last_point_time = time.time()
                    self.recent_count = 0
                else:
                    self.recent_count += 1
                    for j in range(3):
                        self.recent_avg[j] -= self.recent_avg[j] / self.recent_count
                        self.recent_avg[j] += datapoint[j] / self.recent_count

            # the plot data is the summed counts divided by the occurence of the respective bins
            self.histogram = self.rawhisto / self.sumhisto

    def save_data(self, timestamp=None):
        """ Save the counter trace data and writes it to a file.

        @param datetime timestamp: timestamp passed from gui so that saved images match filenames
                                    of data. This will be removed when savelogic handles the image
                                    creation also.

        @return int: error code (0:OK, -1:error)
        """

        self._saving_stop_time = time.time()

        filepath = self._save_logic.get_path_for_module(module_name='WavemeterLogger')
        filelabel = 'wavemeter_log_histogram'

        # Currently need to pass timestamp from gui so that the saved image matches saved data.
        # TODO: once the savelogic saves images, we can revert this to always getting timestamp here.
        if timestamp is None:
            timestamp = datetime.datetime.now()

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Wavelength (nm), Signal (counts/s)': np.array(
                [self.histogram_axis, self.histogram]
                ).transpose()}

        # write the parameters:
        parameters = OrderedDict()
        parameters['Bins (#)'] = self._bins
        parameters['Xmin (nm)'] = self._xmin
        parameters['XMax (nm)'] = self._xmax
        parameters['Start Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                     time.localtime(self._acqusition_start_time)
                                                     )
        parameters['Stop Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                    time.localtime(self._saving_stop_time)
                                                    )

        self._save_logic.save_data(data,
                                   filepath,
                                   parameters=parameters,
                                   filelabel=filelabel,
                                   timestamp=timestamp,
                                   as_text=True,
                                   precision=':.6f'
                                   )

        filelabel = 'wavemeter_log_wavelength'

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Time (s), Wavelength (nm)': self._wavelength_data}
        # write the parameters:
        parameters = OrderedDict()
        parameters['Acquisition Timing (ms)'] = self._logic_acquisition_timing
        parameters['Start Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                     time.localtime(self._acqusition_start_time)
                                                     )
        parameters['Stop Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                    time.localtime(self._saving_stop_time)
                                                    )

        self._save_logic.save_data(data,
                                   filepath,
                                   parameters=parameters,
                                   filelabel=filelabel,
                                   timestamp=timestamp,
                                   as_text=True,
                                   precision=':.6f'
                                   )

        filelabel = 'wavemeter_log_counts'

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Time (s),Signal (counts/s)': self._counter_logic._data_to_save}

        # write the parameters:
        parameters = OrderedDict()
        parameters['Start counting time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._counter_logic._saving_start_time))
        parameters['Stop counting time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss', time.localtime(self._saving_stop_time))
        parameters['Length of counter window (# of events)'] = self._counter_logic._count_length
        parameters['Count frequency (Hz)'] = self._counter_logic._count_frequency
        parameters['Oversampling (Samples)'] = self._counter_logic._counting_samples
        parameters['Smooth Window Length (# of events)'] = self._counter_logic._smooth_window_length

        self._save_logic.save_data(data,
                                   filepath,
                                   parameters=parameters,
                                   filelabel=filelabel,
                                   timestamp=timestamp,
                                   as_text=True,
                                   precision=':.6f'
                                   )

        self.log.debug('Laser Scan saved to:\n{0}'.format(filepath))

        filelabel = 'wavemeter_log_counts_with_wavelength'

        # prepare the data in a dict or in an OrderedDict:
        data = OrderedDict()
        data = {'Measurement Time (s), Signal (counts/s), Interpolated Wavelength (nm)': np.array(self.counts_with_wavelength)}

        fig = self.draw_figure()
        # write the parameters:
        parameters = OrderedDict()
        parameters['Start Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                     time.localtime(self._acqusition_start_time)
                                                     )
        parameters['Stop Time (s)'] = time.strftime('%d.%m.%Y %Hh:%Mmin:%Ss',
                                                    time.localtime(self._saving_stop_time)
                                                    )

        self._save_logic.save_data(data,
                                   filepath,
                                   parameters=parameters,
                                   filelabel=filelabel,
                                   timestamp=timestamp,
                                   as_text=True,
                                   plotfig=fig,
                                   precision=':.6f')
        plt.close(fig)
        return 0

    def draw_figure(self):
        """ Draw figure to save with data file.

        @return: fig fig: a matplotlib figure object to be saved to file.
        """
        # TODO: Draw plot for second APD if it is connected

        wavelength_data = [entry[2] for entry in self.counts_with_wavelength]
        count_data = np.array([entry[1] for entry in self.counts_with_wavelength])

        # Index of max counts, to use to position "0" of frequency-shift axis
        count_max_index = count_data.argmax()

        # Scale count values using SI prefix
        prefix = ['', 'k', 'M', 'G']
        prefix_index = 0

        while np.max(count_data) > 1000:
            count_data = count_data / 1000
            prefix_index = prefix_index + 1

        counts_prefix = prefix[prefix_index]

        # Use qudi style
        plt.style.use(self._save_logic.mpl_qd_style)

        # Create figure
        fig, ax = plt.subplots()

        ax.plot(wavelength_data, count_data, linestyle=':', linewidth=0.5)

        ax.set_xlabel('wavelength (nm)')
        ax.set_ylabel('Fluorescence (' + counts_prefix + 'c/s)')

        x_formatter = mpl.ticker.ScalarFormatter(useOffset=False)
        ax.xaxis.set_major_formatter(x_formatter)

        ax2 = ax.twiny()

        nm_xlim = ax.get_xlim()
        ghz_at_max_counts = self.nm_to_ghz(wavelength_data[count_max_index])
        ghz_min = self.nm_to_ghz(nm_xlim[0]) - ghz_at_max_counts
        ghz_max = self.nm_to_ghz(nm_xlim[1]) - ghz_at_max_counts

        ax2.set_xlim(ghz_min, ghz_max)
        ax2.set_xlabel('Shift (GHz)')

        return fig

    def nm_to_ghz(self, wavelength):
        return 3e8 / wavelength
