class VoiceAgent:
    def __init__(self, name):
        self.name = name

    def speak(self, message):
        print(f"{self.name} says: {message}")