"""LOCKI AIRLOCK camera: what happens at the workstation, as timestamped observations.

camera (CameraSource) -> inference adapter -> trackers (presence, camera health; faces and zones next)
-> rate limit -> SecurityEventV1 through the security publisher.

It reports what the camera saw (someone at the desk, a hand in the USB zone, a covered lens), never who
someone is beyond "matches the enrolled owner", and never what they intend. The Mac's USB watcher stays the
authority on whether a device was actually connected; Sid's SpacetimeDB module decides incidents.
See docs/airlock/VISION.md.
"""
