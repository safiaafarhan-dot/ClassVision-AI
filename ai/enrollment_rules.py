"""
ClassVision AI - Enrollment Capture Rules
-----------------------------------------
The three numbers that decide when an enrollment sample is taken.

They live in their own module for two reasons. They are shared by the
command-line tool and the dashboard, and a copy in each would eventually
drift - one path would quietly become stricter than the other, and two
students enrolled on the same day would have been held to different
standards.

And they are needed by code that must not import OpenCV: the backend reads
them to report progress, and pulling a vision stack into a web process to
learn the number 5 would be a poor trade.

Changing any of these changes what counts as a good enrollment for BOTH
paths, which is the point.
"""

# How many samples make an enrollment. Five poses, so the best-of-five match
# in Step 4 has genuinely different angles to choose between rather than five
# copies of one pose.
DEFAULT_SAMPLES = 5

# Consecutive good frames before a sample is taken. A face that passes the
# quality gate for a single frame was probably passing through.
STABLE_FRAMES_REQUIRED = 6

# Quiet period after a sample, so one steady pose cannot produce five nearly
# identical vectors.
COOLDOWN_SECONDS = 1.5
