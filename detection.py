import os
import cv2
from ultralytics import YOLO
from flask import current_app
from database import get_db
import time
import threading
import queue
import logging
from time import sleep

# Load YOLO model
model_path = "models/yolov11/fall-detection-model.pt"
model = YOLO(model_path)

LABEL_MAP = {
    0: "Jatuh",
    1: "Normal"
}

CONFIDENCE_THRESHOLD = 0.7

_stream_handlers = {}
_handlers_lock = threading.Lock()


class RTSPStreamHandler:
    def __init__(self, source, model, buffer_size=30):
        self.source = source
        self.model = model
        self.frame_buffer = queue.Queue(maxsize=buffer_size)
        self.processed_frame = None
        self.last_frame = None
        self.running = False
        self.lock = threading.Lock()
        self.last_access = time.time()

    def start(self):
        self.running = True
        self.capture_thread = threading.Thread(target=self._capture_frames)
        self.process_thread = threading.Thread(target=self._process_frames)
        self.capture_thread.daemon = True
        self.process_thread.daemon = True
        self.capture_thread.start()
        self.process_thread.start()

    def stop(self):
        self.running = False
        if hasattr(self, 'capture_thread'):
            self.capture_thread.join(timeout=1.0)
        if hasattr(self, 'process_thread'):
            self.process_thread.join(timeout=1.0)

    def _capture_frames(self):
        cap = cv2.VideoCapture(self.source)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)  

        while self.running:
            ret, frame = cap.read()
            if not ret:
                logging.error("Failed to read frame from RTSP stream")
                sleep(0.1)
                continue

            with self.lock:
                self.last_frame = frame.copy()
                self.last_access = time.time()

            # Kurangi frame buffer lama
            while self.frame_buffer.full():
                self.frame_buffer.get_nowait()

            self.frame_buffer.put(frame)

        cap.release()
        
    def _process_frames(self):
        while self.running:
            try:
                frame = self.frame_buffer.get(timeout=1)
                if frame is None:
                    continue
                logging.info(f"Processing frame with shape: {frame.shape}")

                # Proses deteksi dengan model tanpa resize untuk konsistensi
                results = self.model(frame, stream=False) ## tambahan: stream=False

                processed_frame = frame.copy()

                for result in results:
                    if not hasattr(result, "boxes") or result.boxes is None:
                        continue

                    for box in result.boxes:
                        if not hasattr(box, "xyxy") or box.xyxy is None or len(box.xyxy) == 0:
                            continue
                        try:
                            confidence = float(box.conf[0].item())
                            class_id = int(box.cls[0].item())

                            if confidence < CONFIDENCE_THRESHOLD:
                                continue

                            # Konversi bounding box ke integer
                            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())

                            # Ambil label dan warna berdasarkan class_id
                            label = LABEL_MAP.get(class_id, f"Unknown-{class_id}")
                            color = (0, 0, 255) if label.lower() == "jatuh" else (0, 255, 0)

                            # Gambar bounding box dan label
                            cv2.rectangle(processed_frame, (x1, y1), (x2, y2), color, 2)
                            cv2.putText(
                                processed_frame,
                                f"{label} ({confidence:.2f})",
                                (x1, max(0, y1 - 10)),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.5,
                                color,
                                2
                            )
                            if label.lower() == "jatuh":
                                logging.info(f"Fall detected with confidence: {confidence:.2f}")

                        except Exception as e:
                            logging.error(f"Error processing box: {str(e)}")
                            continue

                with self.lock:
                    self.processed_frame = processed_frame
                    self.last_access = time.time()

            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"Error processing frame: {str(e)}")
                continue


    def get_frame(self):
        with self.lock:
            self.last_access = time.time()
            if self.processed_frame is not None:
                return self.processed_frame
            return self.last_frame if self.last_frame is not None else None


def get_stream_handler(video_source, model):
    """Helper function untuk mendapatkan atau membuat stream handler"""
    with _handlers_lock:
        handler = _stream_handlers.get(video_source)
        if handler is None or not handler.running:
            handler = RTSPStreamHandler(video_source, model)
            handler.start()
            _stream_handlers[video_source] = handler
        return handler


def cleanup_handlers(max_idle_time=30):
    """Membersihkan handler yang tidak aktif"""
    with _handlers_lock:
        current_time = time.time()
        inactive = []
        for source, handler in _stream_handlers.items():
            if current_time - handler.last_access > max_idle_time:
                handler.stop()
                inactive.append(source)

        for source in inactive:
            del _stream_handlers[source]


def generate_frames(video_source, user_id):
    cap = cv2.VideoCapture(video_source)
    if not cap.isOpened():
        raise ValueError(
            f"Tidak dapat membuka video atau URL RTSP: {video_source}")
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        try:
            frame = detect_and_label(frame, user_id)
            # Encode frame ke format JPEG
            _, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        except Exception as e:
            print(f"Kesalahan saat memproses frame: {e}")
            break
    cap.release()


def save_frame_with_bbox(frame, frame_count, user_id):
    """
    Menyimpan frame full dengan bounding box untuk laporan email.
    """
    try:
        timestamp = int(time.time())
        image_filename = f"fall_{user_id}_{timestamp}_{frame_count}_bbox.jpg"

        abs_image_path = os.path.join(
            current_app.config['DETECTION_IMAGES_FOLDER'], image_filename)
        rel_image_path = f"uploads/detections/{image_filename}"

        os.makedirs(os.path.dirname(abs_image_path), exist_ok=True)

        success = cv2.imwrite(abs_image_path, frame)
        if not success:
            raise Exception("Failed to save image")

        print(f"Frame saved successfully to: {abs_image_path}")

        return rel_image_path

    except Exception as e:
        print(f"Error saving frame: {str(e)}")
        return None


def save_detection_to_db(user_id, label, confidence, image_path=None):
    """
    Menyimpan data deteksi ke database.
    """
    if label.lower() == "jatuh":
        connection = get_db()
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO detections (user_id, label, confidence, image_path)
            VALUES (%s, %s, %s, %s)
        """, (user_id, label, confidence, image_path))
        connection.commit()
        cursor.close()


def detect_and_label(frame, user_id):
    results = model(frame, stream=False) 
    for result in results:
        if hasattr(result, "boxes") and len(result.boxes) > 0:  # Hanya proses jika ada deteksi
            for box in result.boxes:
                # Ambil bounding box dan informasi deteksi
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                confidence = box.conf[0].item()
                class_id = int(box.cls[0].item())

                if confidence < CONFIDENCE_THRESHOLD:
                    continue  # Abaikan deteksi dengan confidence rendah

                # Ambil nama label berdasarkan indeks
                label = LABEL_MAP.get(class_id, f"Unknown-{class_id}")

                # Simpan ke database
                save_detection_to_db(user_id, label, confidence)

                # Gambar bounding box dan label pada frame
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, f"{label} ({confidence:.2f})", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
    return frame


def process_video(input_path, output_path, user_id, save_for_email=False):
    """
    Memproses video untuk mendeteksi dan menyimpan hasil.
    """
    try:
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open input video: {input_path}")

        fps = int(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            fps = 25
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        output_path = output_path.replace('\\', '/')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if os.path.exists(output_path):
            os.remove(output_path)

        fourcc = cv2.VideoWriter_fourcc(*'avc1')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        if not out.isOpened():
            raise ValueError(f"Cannot create output video: {output_path}")

        frame_count = 0
        highest_confidence = 0.0
        email_frame_path = None

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret or frame is None:
                logging.error("Failed to read frame or frame is None")
                break

            frame_count += 1
            try:
                results = model(frame, stream=False) ## tambahkan stream=False
                for result in results:
                    if hasattr(result, "boxes"):
                        for box in result.boxes:
                            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                            confidence = box.conf[0].item()
                            class_id = int(box.cls[0].item())

                            if confidence <= CONFIDENCE_THRESHOLD:
                                continue

                            label = LABEL_MAP.get(
                                class_id, f"Unknown-{class_id}")
                            cv2.rectangle(frame, (x1, y1),
                                          (x2, y2), (0, 255, 0), 2)
                            cv2.putText(frame, f"{label} ({confidence:.2f})",
                                        (x1, y1 - 10),
                                        cv2.FONT_HERSHEY_SIMPLEX,
                                        0.5,
                                        (0, 255, 0),
                                        2)

                            if label.lower() == "jatuh":
                                try:
                                    timestamp = int(time.time())
                                    detection_folder = os.path.abspath(
                                        current_app.config['DETECTION_IMAGES_FOLDER'])
                                    os.makedirs(detection_folder,
                                                exist_ok=True)

                                    cropped_filename = f"fall_{user_id}_{timestamp}_{frame_count}.jpg"
                                    bbox_filename = f"fall_{user_id}_{timestamp}_{frame_count}_bbox.jpg"

                                    cropped_abs_path = os.path.join(
                                        detection_folder, cropped_filename)
                                    bbox_abs_path = os.path.join(
                                        detection_folder, bbox_filename)

                                    cropped_image = frame[y1:y2, x1:x2]
                                    cv2.imwrite(cropped_abs_path,
                                                cropped_image)

                                    if confidence > highest_confidence and save_for_email:
                                        highest_confidence = confidence
                                        cv2.imwrite(bbox_abs_path, frame)
                                        email_frame_path = f"uploads/detections/{bbox_filename}"
                                        print(
                                            f"Saved bbox image to: {bbox_abs_path}")

                                    rel_path = f"uploads/detections/{cropped_filename}"
                                    save_detection_to_db(
                                        user_id, label, confidence, rel_path)

                                except Exception as save_error:
                                    print(
                                        f"Error saving detection images: {str(save_error)}")
                                    print(
                                        f"Current working directory: {os.getcwd()}")

                out.write(frame)

            except Exception as e:
                print(f"Error on frame {frame_count}: {str(e)}")
                continue

        cap.release()
        out.release()

        print(f"Video processing completed: {output_path}")
        return email_frame_path

    except Exception as e:
        print(f"Fatal error during video processing: {str(e)}")
        raise
