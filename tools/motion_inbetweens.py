"""Offline in-betweening for registered transparent drawings.

Requires numpy and opencv-python-headless when rebuilding artwork. Shipped
frames are ordinary PNGs and playback does not require either library.
"""
import cv2
import numpy as np
from PIL import Image


def cyclic_frames(frames, subdivisions=4):
    """Warp adjacent drawings toward each other, including last to first."""
    if subdivisions < 1:
        raise ValueError("A motion clip needs at least one sample per drawing.")
    # Premultiplication prevents hidden RGB from bleeding into silhouette edges.
    premultiplied = [np.array(frame.convert("RGBa"), dtype=np.float32) for frame in frames]
    # A neutral backing lets flow track white paws and dark outlines equally.
    gray = [cv2.cvtColor(np.clip(frame[:, :, :3] + (255-frame[:, :, 3:4])*0.55,
                                  0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
            for frame in premultiplied]
    flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow.setFinestScale(0)
    height, width = gray[0].shape
    y, x = np.mgrid[:height, :width].astype(np.float32)
    grid = np.dstack((x, y))

    def sample(image, coordinates):
        return cv2.remap(image, coordinates, None, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)

    def advect(image, velocity, t):
        # Flow is known at the source pixel. Invert x + t*v(x) using two
        # refinements so fast swinging toes follow their curved silhouette.
        source = grid - velocity*t
        for _ in range(2):
            source = grid - sample(velocity, source)*t
        return sample(image, source)

    # Weight each interval by the visible travel in the lower legs. Give a
    # longer swing more screen time than two almost identical contacts.
    flows, travel = [], []
    for i, current in enumerate(gray):
        following = gray[(i+1) % len(frames)]
        forward = flow.calc(current, following, None)
        flows.append((forward, flow.calc(following, current, None)))
        legs = (premultiplied[i][:, :, 3] > 128) & (y > height * 0.7)
        distances = np.linalg.norm(forward[legs], axis=1)
        travel.append(np.percentile(distances, 80) if distances.size else 0)
    mean = max(np.mean(travel), 0.01)
    weights = np.clip(0.5 + 0.5*np.array(travel)/mean, 0.75, 1.5)
    boundaries = np.rint(np.cumsum(weights) / weights.sum() * len(frames)*subdivisions).astype(int)
    key_positions = [0, *boundaries[:-1].tolist()]
    result = []
    for i, original in enumerate(frames):
        j = (i+1) % len(frames)
        result.append(original)
        forward, reverse = flows[i]
        samples = int(boundaries[i]) - key_positions[i]
        for step in range(1, samples):
            t = step / samples
            old = advect(premultiplied[i], forward, t)
            new = advect(premultiplied[j], reverse, 1-t)
            # Prefer the nearer drawing where movement reveals previously
            # occluded fur. Never crossfade two unregistered limb positions.
            weight = t*t*(3-2*t)
            # If only one warped silhouette occupies a pixel, blending both
            # leaves a second translucent paw. Choose the closer key there.
            disagreement = np.abs(old[:, :, 3:4] - new[:, :, 3:4])
            occlusion = np.clip((disagreement - 64) / 128, 0, 1)
            blend = weight + occlusion * (float(t >= 0.5) - weight)
            pixels = np.rint(old*(1-blend) + new*blend).astype(np.uint8)
            # A paw passing another leg can leave disconnected islands in the
            # warped silhouette. Keep the connected animal and its soft edge.
            opaque = np.asarray(pixels[:, :, 3] > 30, dtype=np.uint8)
            count, labels, stats, _ = cv2.connectedComponentsWithStats(opaque)
            if count > 2:
                main = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
                body = np.asarray(labels == main, dtype=np.uint8)
                fringe = cv2.dilate(body, np.ones((5, 5), dtype=np.uint8))
                pixels[(fringe == 0) | ((opaque != 0) & (body == 0))] = 0
            result.append(Image.frombytes("RGBa", (width, height), pixels.tobytes()).convert("RGBA"))
    return result, key_positions
