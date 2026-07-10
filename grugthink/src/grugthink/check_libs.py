import numpy as np
import torch

print(f"Numpy version: {np.__version__}")
print(f"Torch version: {torch.__version__}")

try:
    test_array = np.array([1, 2, 3])
    print(f"Numpy array created successfully: {test_array}")
except Exception as e:
    print(f"Error creating Numpy array: {e}")

try:
    test_tensor = torch.tensor([1, 2, 3])
    print(f"Torch tensor created successfully: {test_tensor}")
except Exception as e:
    print(f"Error creating Torch tensor: {e}")

try:
    # Attempt to convert a torch tensor to a numpy array
    numpy_from_torch = test_tensor.numpy()
    print(f"Converted Torch tensor to Numpy array: {numpy_from_torch}")
except Exception as e:
    print(f"Error converting Torch tensor to Numpy array: {e}")
