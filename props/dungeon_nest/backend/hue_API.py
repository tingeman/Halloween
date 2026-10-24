
import time
import colorsys
import os
from phue import Bridge as _Bridge, PhueRegistrationException
import threading
import random
import ipdb


def Bridge(bridge_ip):
    # HUE_CONFIG_FILE lets the registration key live on a persistent volume (default: ~/.python_hue)
    return _Bridge(bridge_ip, config_file_path=os.environ.get('HUE_CONFIG_FILE'))

hue_bridge_ip = '10.67.1.54'
light_pattern = 'halloween'
sensor_pattern = 'bryggers'   # 'halloween'

run_disco = True


class HueBridge:
    def __init__(self, bridge_ip):
        self.bridge_ip = bridge_ip
        self.b = Bridge(bridge_ip)        


class HueLights:

    commands = {'red':         {'transitiontime': 70, 'on':  True, 'bri' : 90, 'hue': 65535, 'sat': 255},
                'yellow':      {'transitiontime': 70, 'on':  True, 'bri':  58, 'hue': 13539, 'sat': 252},
                'pink':        {'transitiontime': 70, 'on':  True, 'bri':  58, 'hue': 58294, 'sat': 253},
                'purple':      {'transitiontime': 70, 'on':  True, 'bri': 136, 'hue': 58294, 'sat': 253},
                'normal':      {'transitiontime': 70, 'on':  True, 'bri': 144, 'hue': 13524, 'sat': 200},
                'normal_lstr': {'transitiontime': 70, 'on':  True, 'bri': 144, 'hue':  7676, 'sat': 199},
                'dimmed':      {'transitiontime': 70, 'on':  True, 'bri':   0}, 
                'off':         {'transitiontime':  0, 'on': False},
                }
                
    disco_cmds = ['red', 'red', 'red', 'red', 'red', 'yellow', 'yellow', 'yellow', 'pink', 'purple']
    disco_durations =  [1,2,3,4]   # in seconds
    disco_brightness = [0, 0, 0, 50, 50, 50, 100, 150]   # in seconds

    def __init__(self, bridge_ip, light_pattern):
        self.run_disco = False
        self.bridge_ip = bridge_ip
        self.b = Bridge(bridge_ip)
                
        # get lights
        lights = self.b.get_light_objects('name')
                
        # Get the appropriate lights (only include reachable ones)
        self.lights = {}
        self.lights_uids = []
        light_pattern = light_pattern.lower()
        
        unreachable_lights = []
        for light_name in lights.keys():
            if light_pattern in light_name.lower():
                light = lights[light_name]
                # Check if light is reachable before adding it
                if light.reachable:
                    self.lights[light_name] = light
                else:
                    unreachable_lights.append(light_name)
        
        # Report unreachable lights that matched the pattern
        if unreachable_lights:
            print(f"Found {len(unreachable_lights)} unreachable light(s) matching pattern '{light_pattern}': {', '.join(unreachable_lights)}")

        self.lights_uids = [l.light_id for l in self.lights.values()]
        self.lights_types = [l.type for l in self.lights.values()]
        self.list_lights()
        
        
    def __len__(self) -> int:
        return len(self.lights)

    def lights_off(self, transitiontime=50):
        self.disco_on = False
        cmd = self.commands['dimmed']
        cmd['transitiontime'] = transitiontime
        self.b.set_light(self.lights_uids, cmd)
        
        timer = threading.Timer(transitiontime/10., self.b.set_light, args=[self.lights_uids, self.commands['off']])
        timer.start()  # after 60 seconds, 'callback' will be called
    
    def send_command(self, cmd=None, uids=None,  **kwargs):
        if uids is None:
            uids = self.lights_uids
            
        if cmd is not None:
            cmd_dict = self.commands[cmd]
            cmd_dict.update(**kwargs)
        else:
            cmd_dict = kwargs
        
        if cmd == 'off':
            self.disco_on = False

        bw_uids = []
        color_uids = []

        for id, uid in enumerate(uids):
            if 'color' in self.lights_types[id]:
                color_uids.append(uid)
            else:
                bw_uids.append(uid)
        
        if len(color_uids)>0:
            self.b.set_light(color_uids, cmd_dict)
        if len(bw_uids)>0:
            # Remove color keys for bw lights
            bw_cmd = {k: v for k, v in cmd_dict.items() if k not in ['hue', 'sat']}
            self.b.set_light(bw_uids, bw_cmd)

    def list_lights(self):
        for id, (n, l) in enumerate(self.lights.items()):
            try:
                print("{0})  {1:30}     'on': {2:>5s}, 'bri': {3:>3d}, 'hue': {4:>5d}, 'sat': {5:>3d}".format(id, n, (lambda x: 'True' if x else 'False')(l.on), l.brightness, l.hue, l.saturation))
            except Exception as e:
                print("{0})  {1:30}     'on': {2:>5s}, 'bri': {3:>3d}".format(id, n, (lambda x: 'True' if x else 'False')(l.on), l.brightness))

    def start_disco(self, uids=None):
        self.disco_on = True
        
        if uids is None:
            uids = self.lights_uids
        
        for uid in uids:
            self._disco_callback(uid)
   
    def _disco_callback(self, uid):
        #print(self.disco_on)
        if not self.disco_on:
            print('stopping disco (uid: {0})'.format(uid))
            return     # return without scheduling new
        
        rcmd = random.choice(self.disco_cmds)
        rtransition = random.choice(self.disco_durations)
        rbri = random.choice(self.disco_brightness)
        
        #print('changing color to {0}  bri: {1}  duration: {2}s       (uid {0})'.format(rcmd,rbri, rtransition, uid))
        self.send_command(uids=[uid], cmd=rcmd, transitiontime=rtransition*5, bri=rbri)
        
        # schedule next light change
        timer = threading.Timer(rtransition, self._disco_callback, args=[uid])
        timer.start()


class HueSensor:
    def __init__(self, bridge_ip, sensor_pattern):
        self.bridge_ip = bridge_ip
        self.b = Bridge(bridge_ip)
                
        # get sensors and lights
        sensors = self.b.get_sensor_objects('name')

        sensor_pattern = sensor_pattern.lower()

        # Get the motion sensor
        self.sensor = None
        self.name = ""
        for sname in sensors.keys():
            if sensor_pattern in sname.lower():
                print('{0}'.format(sname))
                self.sensor = sensors[sname]
                self.name = self.sensor.name
                break

        
        self.refresh()
    
    def refresh(self):
        self.sensor_state = self.b.get_sensor(sensor_id=self.sensor.sensor_id)['state']
        self.presence = self.sensor_state.get('presence', False)
        self.updated = self.sensor_state['lastupdated']

    def has_presence(self):
        return self.sensor_state.get('presence', False)
    


def main():
    global run_disco

    b = Bridge(hue_bridge_ip)

    # If the app is not registered and the button is not pressed, press the button and call connect() (this only needs to be run a single time)
    b.connect()

    # Get the bridge state (This returns the full dictionary that you can explore)
    my_api = b.get_api()
    groups = b.get_group()

    # Connect to Hue system
    print('Connecting to Hue...')
    sensor = HueSensor(hue_bridge_ip, sensor_pattern)
    lights = HueLights(hue_bridge_ip, light_pattern)

    lights.send_command('off')
    
    lights.send_command('red')
    time.sleep(5)
    
    if run_disco:
        lights.start_disco()

    time.sleep(30)
    lights.lights_off(transitiontime=50)


if __name__ == "__main__":
    try:
        main()
    except PhueRegistrationException as e:
        print(e)
        print('Press the button on the Hue bridge and try again...')
    # s
    
