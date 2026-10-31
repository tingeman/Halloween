
"""Chromecast group helper utilities.

This module provides a simple wrapper around ``pychromecast`` for managing
groups of Chromecast devices. It offers both synchronous and non-blocking
background media loading APIs. Key guarantees:

- ``ChromecastGroup.load_media_bg`` returns a ``MediaLoadTask`` which will
    ensure a loaded track is PAUSED when the device reports "ready" unless
    ``autoplay=True`` is specified. This prevents races where a subsequent
    ``play()`` call is issued before the device has reached a stable PLAYING
    state.
- The background task supports cancel, join and asyncio Future bridging via
    ``cancel()``, ``join()`` and ``as_future()``.

Usage examples::

        group = ChromecastGroup(['10.0.0.10'])
        # Non-blocking load that leaves the device paused when ready
        task = group.load_media_bg(['http://host/track.mp3'], autoplay=False)
        # Wait in asyncio code:
        results = await task.as_future()
        # Or poll/join from synchronous code:
        results = task.join()

The module preserves the original synchronous ``load_media`` behaviour
for compatibility.
"""

import numpy as np
import time
import asyncio
import colorsys
import pychromecast
from pychromecast.const import MESSAGE_TYPE
from pychromecast import WaitResponse
import threading
from dataclasses import dataclass
from typing import Optional, List, Callable, Any
import random

import ipdb

from halloween_common import volume_limit

# STRANGE BEHAVIOUR:
# REPEAT only works with a queue.
# To initiate a queue, first load a media.
# This first media has to be playing, in order to add a second
# media and form a queue.
# Once a queue has been formed, we can issue the QUEUE_REPEAT commands.
#
# For example this should work:
# cc_group.load_media(url_list=['http://10.67.1.254:8000/Thriller-Laugh%20-%20original.mp3'], enqueue=False, autoplay=True)
# cc_group.load_media(url_list=['http://10.67.1.254:8000/Thriller-Laugh%20-%20original.mp3'], enqueue=True, autoplay=True)
# cc_group.queue_repeat_single()


# def seek(castunit, position, resumeState="PLAYBACK_PAUSE"):
#     """Seek the media to a specific location."""
#     castunit.media_controller._send_command(
#         {
#             MESSAGE_TYPE: "SEEK",
#             "currentTime": position,
#             "resumeState": resumeState,
#         }
#     )


def seek(castunit, position: float, resumeState: str = "PLAYBACK_PAUSE", timeout: float = 10.0) -> None:
    """Seek the media to a specific location."""
    response_handler = WaitResponse(timeout, f"seek {position}")
    castunit.media_controller._send_command(
        {
            MESSAGE_TYPE: "SEEK",
            "currentTime": position,
            "resumeState": resumeState,
        },
        response_handler.callback,
    )
    response_handler.wait_response()



QUEUE_REPEAT_OFF = "REPEAT_OFF"
QUEUE_REPEAT_ALL = "REPEAT_ALL"
QUEUE_REPEAT_SINGLE = "REPEAT_SINGLE"
QUEUE_REPEAT_ALL_AND_SHUFFLE = "REPEAT_ALL_AND_SHUFFLE"


def queue_repeat(castunit, repeatMode: str, timeout: float = 10.0) -> None:
    """Send QUEUE repeat command."""
    response_handler = WaitResponse(timeout, f"set repeat mode {repeatMode}")
    castunit.media_controller._send_command(
        {
            MESSAGE_TYPE: "QUEUE_UPDATE",
            "resumeMode": repeatMode,
        },
        response_handler.callback,
    )
    response_handler.wait_response()


class ChromecastGroup:
    """Manage a group of Chromecast devices discovered by IP address.

    This class wraps common operations (load, play, pause, stop, volume).

    Important behavior:
    - Use ``load_media`` for synchronous/blocking loads (legacy behavior).
    - Use ``load_media_bg`` to begin a non-blocking load that ensures the
      device is PAUSED when ready unless ``autoplay=True`` is specified.
    - The class relies on ``pychromecast`` for device discovery and control.
    - Every volume change goes through ``_apply_volume`` so the global volume
      limit (``set_volume_limit``) caps each speaker at limit * its default.
    """
    TIMEOUT = 10  # seconds
    VOLUME_STEP = 0.1

    def __init__(self, host_list, volumes=None, volume_limit_scale=volume_limit.NO_LIMIT):
        self.volume_limit = volume_limit_scale
        chromecast_devices, browser = pychromecast.get_chromecasts(known_hosts=host_list)
        if not chromecast_devices:
            print(f'No chromecasts discovered')
        else:
            print('Found {0} devices.'.format(browser.count))
        self.browser = browser
        self.chromecasts = list(filter(lambda item: item.cast_info.host in host_list, chromecast_devices))
        if volumes is not None:
            self.default_volumes = volumes
        else:
            self.default_volumes = [0.3 for n in host_list]
        # Last volume asked for per speaker; the speaker plays min(target, cap)
        self.target_volumes = list(self.default_volumes)
        # Last URL loaded per speaker host, so play() can reload it if the speaker lost its media
        self._last_urls = {}

        for id, cast in enumerate(self.chromecasts):
            # Start socket client's worker thread and wait for initial status update
            cast.wait()
            try:
                cast.media_controller.stop()
            except pychromecast.RequestFailed as e:
                print(f'Failed to stop media controller: {e}')

            self._apply_volume(id, cast, self.default_volumes[id])
            print(f'Found chromecast with name "{cast.cast_info.friendly_name}"')

    # --- volume limit -------------------------------------------------------
    def _default_volume(self, id):
        return self.default_volumes[id] if id < len(self.default_volumes) else 0.3

    def volume_cap(self, id):
        return volume_limit.cap(self._default_volume(id), self.volume_limit)

    def _apply_volume(self, id, cast, volume):
        """Remember `volume` as the target and set min(target, cap) on the speaker."""
        while len(self.target_volumes) <= id:
            self.target_volumes.append(self._default_volume(len(self.target_volumes)))
        self.target_volumes[id] = max(0.0, min(1.0, volume))
        cast.set_volume(min(self.target_volumes[id], self.volume_cap(id)))

    def set_volume_limit(self, scale):
        """Apply a new global limit to every speaker (raising it restores targets)."""
        self.volume_limit = scale
        print(f'Volume limit: {volume_limit.label(scale)}')
        for id, cast in enumerate(self.chromecasts):
            try:
                self._apply_volume(id, cast, self.target_volumes[id] if id < len(self.target_volumes) else self._default_volume(id))
            except Exception as e:
                print(f'Failed to apply volume limit to {cast.cast_info.host}: {e}')

    def enforce_volume_limit(self):
        """Lower any speaker that is above its cap (e.g. changed in the Google Home app)."""
        if self.volume_limit >= volume_limit.NO_LIMIT:
            return
        for id, cast in enumerate(self.chromecasts):
            try:
                level = cast.status.volume_level if cast.status is not None else None
                if level is not None and level > self.volume_cap(id) + 0.01:
                    cast.set_volume(self.volume_cap(id))
            except Exception as e:
                print(f'Failed to enforce volume limit on {cast.cast_info.host}: {e}')

    def describe(self):
        """Per-speaker status lines, keyed by a topic-safe speaker name."""
        out = {}
        for id, cast in enumerate(self.chromecasts):
            try:
                name = cast.cast_info.friendly_name or cast.cast_info.host
                key = ''.join(c if c.isalnum() or c in '-_' else '_' for c in name)
                player = cast.media_controller.status.player_state
                level = cast.status.volume_level if cast.status is not None else None
                vol = f"{level:.2f}" if level is not None else "?"
                out[key] = f"{cast.cast_info.host} {player} vol {vol} (cap {self.volume_cap(id):.2f})"
            except Exception as e:
                out[f"speaker{id}"] = f"status unavailable: {e}"
        return out

    #def __del__(self):
    #    self.browser.stop_discovery()

    def is_empty(self):
        return len(self.chromecasts) == 0

    def _load_media_for_cast(self, cast, url: str, enqueue: bool, autoplay: bool, cancel_event: Optional[threading.Event] = None):
        """Internal helper to load media onto a single cast.

        This method blocks until the cast reports a stable state (PAUSED/PLAYING) or until
        TIMEOUT expires. If cancel_event is provided and set, the operation will abort early.
        """
        initial_player_state = cast.media_controller.status.player_state

        print("Loading media: " + cast.cast_info.host)
        self._last_urls[cast.cast_info.host] = url
        cast.media_controller.play_media(url, "audio/mp3", autoplay=autoplay, enqueue=enqueue)

        if (not autoplay) and (initial_player_state != 'PLAYING'):
            print("Waiting for paused status: " + cast.cast_info.host)
        else:
            print("Waiting for playing status: " + cast.cast_info.host)

        current_time = time.monotonic()
        while cast.media_controller.status.player_state in ['UNKNOWN', 'IDLE', 'BUFFERING']:
            if cancel_event is not None and cancel_event.is_set():
                print(f'Cancelled load for {cast.cast_info.host} during buffering wait')
                return 'cancelled'
            cast.media_controller.update_status()
            print(cast.media_controller.status.player_state)
            if time.monotonic() - current_time > self.TIMEOUT:
                break
            time.sleep(0.2)

        # Ensure we have an up-to-date status
        cast.media_controller.update_status()

        # If autoplay is False, ensure the device is paused when ready.
        if not autoplay and (initial_player_state != 'PLAYING'):
            # If device says PAUSED we're good; otherwise try to pause and verify.
            if cast.media_controller.status.player_state == 'PAUSED':
                print("Paused status reported: " + cast.cast_info.host)
                return 'paused'
            # Try to pause explicitly (some devices start playing briefly)
            print(f'Enforcing pause on {cast.cast_info.host}')
            try:
                cast.media_controller.pause()
            except Exception as e:
                print(f'Pause failed for {cast.cast_info.host}: {e}')

            verify_deadline = time.monotonic() + self.TIMEOUT
            while time.monotonic() < verify_deadline:
                if cancel_event is not None and cancel_event.is_set():
                    print(f'Cancelled load for {cast.cast_info.host} during pause verification')
                    return 'cancelled'
                cast.media_controller.update_status()
                if cast.media_controller.status.player_state == 'PAUSED':
                    print("Paused status verified: " + cast.cast_info.host)
                    return 'paused'
                time.sleep(0.2)

            # If we reach here, pause verification failed
            print(f'Failed to verify PAUSED for {cast.cast_info.host}; state={cast.media_controller.status.player_state}')
            return 'unexpected'
        else:
            # autoplay True path: if device reported PAUSED but autoplay requested, issue play
            if cast.media_controller.status.player_state == 'PLAYING':
                print("Playing status reported: " + cast.cast_info.host)
                return 'playing'
            elif cast.media_controller.status.player_state == 'PAUSED':
                print("Paused status reported: " + cast.cast_info.host)
                if autoplay:
                    print("Sending play request: " + cast.cast_info.host)
                    try:
                        cast.media_controller.play()
                    except Exception as e:
                        print(f'Play failed for {cast.cast_info.host}: {e}')
                return cast.media_controller.status.player_state

        print(" ")
        print("Debug info:")
        print(f"Host: {cast.cast_info.host}")
        print(f"Host status: {cast.media_controller.status}")
        print(f"Host initial player state: {initial_player_state}")
        print(f"Host player state: {cast.media_controller.status.player_state}")
        print(f"Audoplay requested: {autoplay}")
        raise ValueError('Unexpected player state')

    def load_media(self, url_list, enqueue=False, autoplay=False):
        """Synchronous load_media kept for compatibility.

        Parameters
        - url_list: list of URLs. If fewer URLs than devices are provided, the
          last URL is reused for remaining devices.
        - enqueue: whether to enqueue on the device's queue (bool).
        - autoplay: whether the cast should start playing immediately (bool).

        This function blocks until each device reports a stable state or the
        per-group TIMEOUT expires. Prefer ``load_media_bg`` in async or
        latency-sensitive contexts.
        """
        print('my_chromecast::load_media:  Number of chromecasts: {0}'.format(len(self.chromecasts)))
        for cid, cast in enumerate(self.chromecasts):
            print('my_chromecast::load_media:  {0}: {1}'.format(cid, self.chromecasts[cid].cast_info.host))

            if cid >= len(url_list):
                url = url_list[-1]  # take last item, if there is not enough
            else:
                url = url_list[cid]

            self._load_media_for_cast(cast, url, enqueue=enqueue, autoplay=autoplay)

    @dataclass
    class MediaLoadTask:
        """Background media loader control object.

        Fields
        - group: owning ChromecastGroup
        - url_list: list of URLs to load (one per device ideally)
        - enqueue: whether to enqueue on the cast queue
        - autoplay: whether to start playback immediately

        Methods
        - start(): begin the background load in a daemon thread
        - cancel(): request task cancellation
        - join(timeout=None): wait for completion and return per-device results
        - as_future(loop=None): return an asyncio.Future that resolves with
          the per-device results when loading finishes
        """
        group: 'ChromecastGroup'
        url_list: List[str]
        enqueue: bool
        autoplay: bool
        thread: Optional[threading.Thread] = None
        cancel_event: Optional[threading.Event] = None
        _done_event: Optional[threading.Event] = None
        _exception: Optional[BaseException] = None
        _results: Optional[List[Any]] = None

        def start(self):
            """Start the background load runner.

            The runner iterates the group's devices, loads media for each, and
            records the outcome. The thread is created as a daemon so it won't
            block process exit.
            """
            if self.cancel_event is None:
                self.cancel_event = threading.Event()
            if self._done_event is None:
                self._done_event = threading.Event()
            self._results = []

            def _runner():
                try:
                    for cid, cast in enumerate(self.group.chromecasts):
                        if self.cancel_event.is_set():
                            print('MediaLoadTask cancelled before loading next cast')
                            break

                        if cid >= len(self.url_list):
                            url = self.url_list[-1]
                        else:
                            url = self.url_list[cid]

                        res = self.group._load_media_for_cast(cast, url, enqueue=self.enqueue, autoplay=self.autoplay, cancel_event=self.cancel_event)
                        self._results.append((cast.cast_info.host, res))
                except Exception as e:
                    self._exception = e
                finally:
                    self._done_event.set()

            self.thread = threading.Thread(target=_runner, daemon=True)
            self.thread.start()

        def cancel(self):
            """Request cancellation of the running background task.

            The worker checks the cancel event between devices and while polling
            a device's status; setting this event will cause the runner to
            abort early where possible.
            """
            if self.cancel_event is not None:
                self.cancel_event.set()

        def join(self, timeout: Optional[float] = None):
            """Block until the background task finishes or until timeout.

            Returns the per-device results or raises any exception captured
            during the runner. If the task is not yet done, returns None.
            """
            if self.thread is not None:
                self.thread.join(timeout)
            if self._done_event is not None and self._done_event.is_set():
                if self._exception:
                    raise self._exception
                return self._results
            return None

        def as_future(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Future:
            """Return an asyncio.Future that completes when the task finishes.

            If called from an asyncio context, pass loop=None to use the running loop.
            """
            if loop is None:
                loop = asyncio.get_event_loop()
            fut = loop.create_future()

            def _wait_and_set():
                self._done_event.wait()
                if self._exception:
                    loop.call_soon_threadsafe(fut.set_exception, self._exception)
                else:
                    loop.call_soon_threadsafe(fut.set_result, self._results)

            waiter = threading.Thread(target=_wait_and_set, daemon=True)
            waiter.start()
            return fut

    def load_media_bg(self, url_list, enqueue=False, autoplay=False) -> 'ChromecastGroup.MediaLoadTask':
        """Start loading media in background threads. Returns a MediaLoadTask with control methods.

        The background loader will ensure that when a cast reports 'ready' the track is PAUSED
        unless autoplay=True (in which case it will allow/ensure PLAYING).
        """
        task = ChromecastGroup.MediaLoadTask(group=self, url_list=url_list, enqueue=enqueue, autoplay=autoplay)
        task.start()
        return task

    def stop(self):
        for cast in self.chromecasts: 
            print("stop playing: "+cast.cast_info.host)
            try:
                cast.media_controller.stop()
            except pychromecast.RequestFailed as e:
                print(f'Failed to stop media controller: {e}')

    def play(self):
        self.enforce_volume_limit()
        for cast in self.chromecasts:
            print("start playing: "+cast.cast_info.host)
            controller = cast.media_controller
            try:
                controller.update_status()
            except Exception as e:
                print(f'Status refresh failed for {cast.cast_info.host}: {e}')
            url = self._last_urls.get(cast.cast_info.host)
            if controller.status.player_state in ("IDLE", "UNKNOWN") and url:
                # The preloaded media never became ready (or was dropped): load it and play at once
                print(f"No media ready on {cast.cast_info.host}; reloading and playing")
                controller.play_media(url, "audio/mp3", autoplay=True)
            else:
                controller.play()

    def pause(self):
        for cast in self.chromecasts: 
            print("pausing: "+cast.cast_info.host)
            cast.media_controller.pause()

    def seek(self, position=0, resume_state='PLAYBACK_PAUSE'):
        for cast in self.chromecasts: 
            seek(cast, position, resumeState=resume_state)

    def refresh(self):
        for cast in self.chromecasts:
            cast.media_controller.update_status()
        self.enforce_volume_limit()

    def set_volume(self, volume=None):
        if volume is not None:
            print("Setting all volumes to : {0:.1f}".format(volume))
        else:
            print('Setting default volumes')

        for id, cast in enumerate(self.chromecasts):
            if volume is None:
                self._apply_volume(id, cast, self._default_volume(id))
            else:
                self._apply_volume(id, cast, volume)

    def _current_volume(self, id, cast):
        level = cast.status.volume_level if cast.status is not None else None
        return level if level is not None else self.target_volumes[id]

    def volume_up(self):
        for id, cast in enumerate(self.chromecasts):
            self._apply_volume(id, cast, min(self._current_volume(id, cast) + self.VOLUME_STEP, self.volume_cap(id)))

    def volume_down(self):
        for id, cast in enumerate(self.chromecasts):
            self._apply_volume(id, cast, self._current_volume(id, cast) - self.VOLUME_STEP)


    def any_playing(self):
        result = False
        for cast in self.chromecasts: 
            result = result or (cast.media_controller.status.player_state == 'PLAYING')
        return result

    def all_playing(self):
        result = True
        for cast in self.chromecasts: 
            result = result and (cast.media_controller.status.player_state == 'PLAYING')
        return result

    def any_paused(self):
        result = False
        for cast in self.chromecasts: 
            result = result or (cast.media_controller.status.player_state == 'PAUSED')
        return result

    def any_unknown(self):
        result = False
        for cast in self.chromecasts: 
            result = result or (cast.media_controller.status.player_state == 'UNKNOWN')
        return result

    def state(self):
        # return PLAYING if any device is playing
        # else BUFFERING if any device is buffering
        # else PAUSED if any device is paused
        # else STOPPED if all devices are stopped
        # else UNKNOWN
        state = 'STOPPED'
        for cast in self.chromecasts:
            if cast.media_controller.status.player_state == 'PLAYING':
                return 'PLAYING'
            elif cast.media_controller.status.player_state == 'BUFFERING':
                state = 'BUFFERING'
            elif cast.media_controller.status.player_state == 'PAUSED':
                if state != 'BUFFERING':
                    state = 'PAUSED'
            elif cast.media_controller.status.player_state == 'UNKNOWN':
                if state not in ['BUFFERING', 'PAUSED']:
                    state = 'UNKNOWN'   
        return state

    def queue_repeat_single(self):
        for cast in self.chromecasts: 
            queue_repeat(cast, QUEUE_REPEAT_SINGLE)

    def queue_repeat_all(self):
        for cast in self.chromecasts: 
            queue_repeat(cast, QUEUE_REPEAT_ALL)
            
    def queue_repeat_off(self):
        for cast in self.chromecasts: 
            queue_repeat(cast, QUEUE_REPEAT_OFF)          

    def fade_to_stop(self, duration=5):
        self.refresh()

        max_vol = 0
        for cast in self.chromecasts:  
            if cast.status.volume_level > max_vol:
                max_vol = np.round(cast.status.volume_level, 2)

        fade_delay = 0.2 # duration/(max_vol*10) 
        while max_vol > 0:
            self.refresh()
            max_vol = 0
            for cast in self.chromecasts:
                if cast.media_controller.status.player_state == 'PLAYING':
                    if cast.status.volume_level > 0:
                        current_vol = cast.status.volume_level-0.1
                        cast.set_volume(current_vol)
                    else:
                        current_vol = 0
                    if (current_vol > 0) & (current_vol > max_vol):
                        max_vol = np.round(current_vol, 2)
            time.sleep(fade_delay)

        self.stop()
        for id, cast in enumerate(self.chromecasts):
            print('Resetting chromecast to default volume: '+cast.cast_info.host)
            self._apply_volume(id, cast, self._default_volume(id))

    def play_halloween2023(self):
        url = 'http://10.67.1.254:8000/Halloween%20soundtrack2023.mp3'
        self.load_media([url])
        time.sleep(3)
        self.play()

    #def play_thriller2022(self):
    #    url = 'http://10.67.1.254:32469/object/d463b8bdaf56cc8c9aea/file.mp3'
    #    await self.play_url(url, volume=volume)

    #def play_laugh2022(self):
    #    url = 'http://10.67.1.254:32469/object/feee8acf6e41a67b3ba9/file.mp3'
    #    await self.play_url(url, volume=volume)



if __name__ == "__main__":
    print('Connecting to Chromecasts...')    
    CHROMECAST_GREETER_IP = '10.67.1.252'
    CHROMECAST_BBQ_SKELETON_IP = '10.67.1.251'
    CHROMECAST_UNDECIDED_IP = '10.67.1.250'
    CHROMECAST_DUNGEON_IP = '10.67.1.253'

    KNOWN_HOSTS = [CHROMECAST_UNDECIDED_IP]

    group = ChromecastGroup(KNOWN_HOSTS)
    group.play_halloween2023()
