# 🤖 AI-Powered Exam Proctoring System

An AI-driven online examination monitoring platform that leverages **Computer Vision, Behavioral Analysis, Audio Processing, and Real-Time Event Detection** to ensure examination integrity during remote assessments.

The system continuously monitors candidates through webcam, microphone, browser activity, and user interactions to automatically detect suspicious behavior, generate real-time alerts, and provide administrators with comprehensive monitoring capabilities.

---

# 📖 Overview

Traditional online examinations rely heavily on manual invigilation, making it difficult to prevent cheating at scale. This project introduces an intelligent AI-powered proctoring system capable of automatically monitoring candidates and detecting suspicious activities throughout an online examination.

The system combines multiple Artificial Intelligence techniques including face recognition, computer vision, object detection, behavioral analysis, audio analysis, and browser activity monitoring to create a secure online examination environment.

---

# ✨ Features

## 👨‍🎓 Student Module

- Student Registration & Login
- Secure Authentication
- Face Enrollment & Verification
- Government ID Verification
- System Compatibility Check
- AI-Monitored Online Examination
- Automatic Exam Submission
- Live Exam Monitoring

---

## 👨‍💼 Admin Module

- Administrator Login
- Live Student Monitoring Dashboard
- Real-Time Violation Alerts
- Student Database Management
- Evidence Screenshot Viewer
- Exam Monitoring Dashboard
- Audit Logs

---

# 🤖 AI Detection Modules

The system continuously monitors students using multiple AI models.

| Detection Module | Description |
|------------------|-------------|
| 👤 Face Detection | Detects whether the candidate is present |
| 😀 Face Recognition | Verifies candidate identity using LBPH |
| 👥 Multiple Person Detection | Detects unauthorized individuals |
| 📱 Mobile Phone Detection | Detects mobile phones during exams |
| 👀 Eye Tracking | Monitors candidate attention |
| 🧠 Head Pose Estimation | Detects excessive head movement |
| 🗣 Mouth Detection | Detects talking during examination |
| 🔊 Audio Monitoring | Detects suspicious voice activity |
| 🖥 Tab Switching Detection | Detects browser switching attempts |
| ⌨ Keystroke Dynamics | Monitors abnormal keyboard activity |

---

# 🏗 System Architecture

```
                        Student
                           │
                           ▼
                 Webcam + Microphone
                           │
                           ▼
               AI Detection Engine
        ┌─────────────────────────────┐
        │ Face Detection              │
        │ Face Recognition            │
        │ Phone Detection             │
        │ Eye Tracking                │
        │ Head Pose Estimation        │
        │ Mouth Detection             │
        │ Audio Monitoring            │
        │ Tab Switching Detection     │
        │ Keystroke Analysis          │
        └─────────────────────────────┘
                           │
                           ▼
                 Violation Detection
                           │
                           ▼
                Evidence Screenshot
                           │
                           ▼
                     Flask Backend
                           │
                           ▼
                     SQL Database
                           │
                           ▼
                  Admin Dashboard
```

---

# 🛠 Technology Stack

## Backend

- Python 3.10
- Flask
- Werkzeug

## Database

- SQLAlchemy
- SQLite
- PostgreSQL (Railway Ready)

## Artificial Intelligence

- OpenCV
- Google MediaPipe
- LBPH Face Recognition
- BlazeFace
- Face Mesh

## Audio Processing

- NumPy
- SciPy

## Frontend

- HTML5
- CSS3
- JavaScript
- Jinja2 Templates

## Deployment

- Railway
- Gunicorn
- Procfile
- Headless Linux Support

---

# 📂 Project Structure

```
ai-powered-exam-proctoring-system
│
├── app.py
├── database.py
├── Procfile
├── railway.toml
├── requirements.txt
├── runtime.txt
├── ALGORITHMS.md
├── README_DEPLOY.md
├── run.bat
│
├── modules
│   ├── vision.py
│   ├── face_recog.py
│   ├── audio.py
│   ├── os_monitor.py
│   └── face_landmarker.task
│
├── templates
│   ├── index.html
│   ├── login.html
│   ├── student_login.html
│   ├── student_signup.html
│   ├── student.html
│   ├── check.html
│   ├── verify_face.html
│   ├── verify_id.html
│   ├── exam.html
│   ├── results.html
│   ├── admin_login.html
│   ├── admin_signup.html
│   ├── admin.html
│   └── admin_database.html
│
└── static
    ├── css
    ├── js
    ├── img
    └── evidence
```

---

# 🚀 Installation

Clone the repository.

```bash
git clone https://github.com/sagarikakandula/ai-powered-exam-proctoring-system.git
```

Navigate into the project.

```bash
cd ai-powered-exam-proctoring-system
```

Install dependencies.

```bash
pip install -r requirements.txt
```

Run the application.

```bash
python app.py
```

Open your browser.

```
http://127.0.0.1:5000
```

---

# 📸 Screenshots

> Add screenshots here after uploading them.

- Landing Page
- Student Login
- Face Verification
- Student Dashboard
- Live Examination
- Admin Dashboard
- Alert Notifications
- Database Management

---

# 🔥 Key Highlights

- AI-powered online examination monitoring
- Multi-layer cheating detection
- Real-time administrator alerts
- Automatic evidence generation
- Face verification before examination
- Identity verification support
- Browser activity monitoring
- Audio activity monitoring
- Keystroke behavior analysis
- Cloud deployment using Railway

---

# 🎯 Future Improvements

- Deep Learning based emotion detection
- Suspicious behavior prediction
- Mobile application
- Multi-camera support
- AI-generated examination reports
- Face anti-spoofing
- Voice biometrics
- Automatic attendance generation

---

# 👩‍💻 Author

**Sagarika Kandula**

Aspiring Data Scientist | AI & ML Undergraduate

- LinkedIn: https://www.linkedin.com/in/sagarikakandula
- GitHub: https://github.com/sagarikakandula

---

# 📄 License

This project is developed for educational and research purposes. Feel free to use, modify, and improve it with appropriate attribution.

---

⭐ **If you found this project useful, consider giving it a star!**
