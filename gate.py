import time
import board
import busio
import numpy as np
import cv2
import adafruit_mlx90640
import configparser
from scipy.spatial import distance as dist
from collections import OrderedDict


# Parameters
calibration = 2.0
MIN_BLOB_SIZE = 3
# Set up I2C and sensor
i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
mlx = adafruit_mlx90640.MLX90640(i2c)
mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ


def load_gate_lines(config_file='config.ini'):
    config = configparser.ConfigParser()
    config.read(config_file)

    entry_lines = []
    exit_lines = []

    for key, val in config.items('EntryLines'):
        axis, coord = val.split('=')
        entry_lines.append((axis.strip(), float(coord.strip())))

    for key, val in config.items('ExitLines'):
        axis, coord = val.split('=')
        exit_lines.append((axis.strip(), float(coord.strip())))
    return entry_lines, exit_lines

entry_lines, exit_lines = load_gate_lines('config.ini')

class CentroidTracker:
    def __init__(self, max_disappeared=5):
        self.next_object_id = 0
        self.objects = OrderedDict()
        self.disappeared = OrderedDict()
        self.track_history = OrderedDict()
        self.max_disappeared = max_disappeared

    def register(self, centroid):
        self.objects[self.next_object_id] = centroid
        self.disappeared[self.next_object_id] = 0
        self.track_history[self.next_object_id] = [centroid]
        self.next_object_id += 1

    def deregister(self, object_id):
        del self.objects[object_id]
        del self.disappeared[object_id]
        del self.track_history[object_id]

    def update(self, input_centroids):
        if len(self.objects) == 0:
            for centroid in input_centroids:
                self.register(centroid)
            return self.objects

        object_ids = list(self.objects.keys())
        object_centroids = list(self.objects.values())
        object_array = np.array(object_centroids).reshape(-1, 2)
        input_array = np.array(input_centroids).reshape(-1, 2)

        # Handle empty input centroid list gracefully
        if input_array.shape[0] == 0:
            for object_id in object_ids:
                self.disappeared[object_id] += 1
                if self.disappeared[object_id] > self.max_disappeared:
                    self.deregister(object_id)
            return self.objects

        # Compute distance between existing objects and new input centroids
        D = dist.cdist(object_array, input_array)

        rows = D.min(axis=1).argsort()
        cols = D.argmin(axis=1)[rows]

        used_rows = set()
        used_cols = set()

        # Match existing objects to new input centroids
        for row, col in zip(rows, cols):
            if row in used_rows or col in used_cols:
                continue
            object_id = object_ids[row]
            self.objects[object_id] = input_centroids[col]
            self.track_history[object_id].append(input_centroids[col])
            self.disappeared[object_id] = 0
            used_rows.add(row)
            used_cols.add(col)

        # Handle disappeared objects
        unused_rows = set(range(len(object_centroids))) - used_rows
        for row in unused_rows:
            object_id = object_ids[row]
            self.disappeared[object_id] += 1
            if self.disappeared[object_id] > self.max_disappeared:
                self.deregister(object_id)

        # Register new objects for unmatched input centroids
        unused_cols = set(range(len(input_centroids))) - used_cols
        for col in unused_cols:
            self.register(input_centroids[col])

        return self.objects


entry_count = 0
exit_count = 0

def crossed_line(prev, curr, axis, coord):
    prev_val = prev[1] if axis == 'y' else prev[0]
    curr_val = curr[1] if axis == 'y' else curr[0]
    return (prev_val < coord <= curr_val) or (prev_val > coord >= curr_val)

def detect_blobs(frame, calibration):
    arr = np.array(frame).reshape((24, 32))
    rounded_arr = np.round(arr).astype(int)
    mode_temp = np.bincount(rounded_arr.flatten()).argmax()
    dynamic_threshold = mode_temp + calibration
    mask = arr > dynamic_threshold
    mask_uint8 = (mask * 255).astype(np.uint8)
    contours, _ = cv2.findContours(mask_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    centroids = []
    for cnt in contours:
        if cv2.contourArea(cnt) > MIN_BLOB_SIZE:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cX = int(M["m10"] / M["m00"])
                cY = int(M["m01"] / M["m00"])
                centroids.append((cX, cY))
    print(centroids)
    return centroids

if __name__ == "__main__":
    tracker = CentroidTracker()
    frame_buffer = np.zeros((24 * 32,))

    # Tracking counted objects to avoid double counting
    counted_entries = set()
    counted_exits = set()

    entry_count = 0
    exit_count = 0

    print("Starting thermal people counter. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                mlx.getFrame(frame_buffer)
            except ValueError:
                continue

            centroids = detect_blobs(frame_buffer, calibration)
            tracker.update(centroids)

            for object_id, history in tracker.track_history.items():
                if len(history) >= 2:
                    prev_pos = history[-2]
                    curr_pos = history[-1]

                    for axis, coord in entry_lines:
                        if object_id not in counted_entries and crossed_line(prev_pos, curr_pos, axis, coord):
                            entry_count += 1
                            counted_entries.add(object_id)
                            print(f"Object {object_id} entered via {axis}={coord}")

                    for axis, coord in exit_lines:
                        if object_id not in counted_exits and crossed_line(prev_pos, curr_pos, axis, coord):
                            exit_count += 1
                            counted_exits.add(object_id)
                            print(f"Object {object_id} exited via {axis}={coord}")

            print(f"Live person count: {len(tracker.objects)}, Entries: {entry_count}, Exits: {exit_count}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Exiting...")
