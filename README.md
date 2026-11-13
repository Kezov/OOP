# ghostvoice

A ghostly voice filter for Discord, built on the **reverse → reverb → reverse**
trick: every word swells up out of a spectral wash *before* you say it.

## How it works

The studio version of this effect flips a recording backwards, adds reverb,
and flips it forwards again, which puts the reverb tail *in front of* each sound.
A live mic can't be played backwards, but reversing commutes with convolution:

```
reverse( reverb( reverse(x) ) )  ==  x convolved with reverse(reverb_impulse)
```

So the filter convolves your mic with a **time-reversed reverb impulse response**,
which works in real time. The cost is a delay equal to the swell length (default
0.45 s), because the swell has to play before the word it leads into. The test
suite checks that the live filter matches the literal three-step version sample
for sample.

```
ghostvoice/
  ghost.py      the effect (live GhostFilter + literal offline version)
  convolver.py  low-latency partitioned FFT convolution
  ir.py         synthetic reverb generator / IR file loading
  realtime.py   mic -> filter -> output device
  cli.py        command line
```

## Install

Python 3.9+.

```bash
pip install -e .
# Linux only, if you see "PortAudio library not found":
sudo apt install libportaudio2
```

## Hooking it up to Discord

Discord can't load audio plugins, so ghostvoice plays the filtered voice into a
**virtual audio cable**, and Discord uses that cable as its microphone.

1. **Install a virtual cable**
   - Windows: [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
   - macOS: [BlackHole 2ch](https://existential.audio/blackhole/)
   - Linux (PulseAudio/PipeWire):
     ```bash
     pactl load-module module-null-sink sink_name=ghostvoice sink_properties=device.description=ghostvoice
     pactl load-module module-remap-source master=ghostvoice.monitor source_name=ghostmic source_properties=device.description=GhostMic
     ```
2. **Find your devices:** `ghostvoice devices`
3. **Run the filter** with your real mic as input and the cable as output:
   ```bash
   ghostvoice run -i "Microphone" -o "CABLE Input"   # Windows
   ghostvoice run -i "MacBook" -o "BlackHole"         # macOS
   ghostvoice run -o ghostvoice                       # Linux
   ```
   Names are matched by substring; device numbers work too.
4. **In Discord** → User Settings → Voice & Video:
   - Input Device: `CABLE Output` / `BlackHole 2ch` / `GhostMic`
   - Turn **off** Noise Suppression (Krisp) and Echo Cancellation: they treat the
     ghostly wash as noise and strip it out.
   - Lower the input sensitivity threshold, or use push-to-talk. Hold push-to-talk
     about half a second after you finish speaking, because of the delay.

To hear the effect yourself first, leave out `-o` to play it through your
speakers (use headphones, or it will feed back).

## Tuning

| Option        | What it does                                              | Default |
|---------------|-----------------------------------------------------------|---------|
| `--preset`    | `ghost`, `whisper`, `phantom`, `banshee`                  | `ghost` |
| `--tail`      | Swell length in seconds. **Also the voice delay.**        | 0.45    |
| `--decay`     | Reverb decay (RT60). Longer = fuller, denser swell        | 1.2     |
| `--mix`       | 0 = normal voice, 1 = only the ghostly wash               | 0.7     |
| `--highpass`  | Thin out the wash below this Hz (0 = off)                 | 250     |
| `--lowpass`   | Darken the wash above this Hz (0 = off)                   | 9000    |
| `--gain`      | Output gain in dB                                         | 0       |
| `--ir`        | Use your own reverb impulse response WAV                  | built-in|
| `--block`     | Audio buffer size; raise it if you hear dropouts          | 256     |

## Try it on a recording

```bash
ghostvoice file my_voice.wav ghostly.wav --preset phantom
```

## Development

```bash
pip install -e ".[dev]"
pytest
```
