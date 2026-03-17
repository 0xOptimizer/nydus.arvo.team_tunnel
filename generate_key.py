import os
from cryptography.fernet import Fernet

ENV_FILE = os.path.join(os.path.dirname(__file__), '.env')
KEY_NAME = 'DB_ENCRYPTION_KEY'

def generate_and_insert():
    key = Fernet.generate_key().decode()

    if not os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'w') as f:
            f.write(f"{KEY_NAME}={key}\n")
        print(f"Created .env and wrote {KEY_NAME}.")
        return

    with open(ENV_FILE, 'r') as f:
        lines = f.readlines()

    for line in lines:
        if line.startswith(f"{KEY_NAME}="):
            print(f"{KEY_NAME} already exists in .env. Aborting to avoid overwriting.")
            print("Delete the existing entry manually if you intend to rotate the key.")
            return

    with open(ENV_FILE, 'a') as f:
        if lines and not lines[-1].endswith('\n'):
            f.write('\n')
        f.write(f"{KEY_NAME}={key}\n")

    print(f"Appended {KEY_NAME} to .env.")

if __name__ == '__main__':
    generate_and_insert()