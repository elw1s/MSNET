import cv2
import os

# specify the directory containing the videos
video_dir = './videos'

# specify the directory where the images will be saved
img_dir = './images'

# create the img directory if it doesn't exist
if not os.path.exists(img_dir):
    os.makedirs(img_dir)

# loop through each video file in the directory
for video_file in os.listdir(video_dir):
    if video_file.endswith('.mp4') or video_file.endswith('.avi'): # specify the video file extensions
        # open the video file
        cap = cv2.VideoCapture(os.path.join(video_dir, video_file))

        # get the total number of frames in the video
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # set the frame index to capture every 10th frame
        frame_index = 0

        # loop through each frame in the video
        for i in range(total_frames):
            # capture the current frame
            ret, frame = cap.read()

            # if the frame was successfully captured and it's the 10th frame
            if ret and i % 40 == 0:
                # save the frame as a JPG image
                img_file = f"{frame_index:04d}.jpg"
                cv2.imwrite(os.path.join(img_dir, img_file), frame)

                # increment the frame index
                frame_index += 1

        # release the video file
        cap.release()
